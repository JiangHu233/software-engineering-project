from __future__ import annotations

import hashlib
import json
import uuid
from datetime import timedelta

from flask import g, jsonify, request

from ..extensions import db
from ..models import IdempotencyRecord
from ..services.timeutil import now
from .errors import ApiError, validation_failed


def _is_uuid4(value: str) -> bool:
    try:
        return uuid.UUID(value).version == 4
    except (ValueError, AttributeError, TypeError):
        return False


def require_key() -> tuple[str, str]:
    """校验 Idempotency-Key（§3.7）：缺失/非 UUIDv4 → 400。返回 (key, request_hash)。"""
    key = (request.headers.get("Idempotency-Key") or "").strip()
    if not key:
        raise validation_failed({"Idempotency-Key": ["写接口必须携带 Idempotency-Key 请求头"]})
    if not _is_uuid4(key):
        raise validation_failed({"Idempotency-Key": ["Idempotency-Key 必须为 UUID v4"]})
    body_hash = hashlib.sha256(request.get_data(cache=True) or b"").hexdigest()
    return key, body_hash


def precheck(actor_id: int, key: str, body_hash: str):
    """锁外预检：IN_PROGRESS→409；DONE 同体重放；DONE 异体→422。返回重放响应或 None。"""
    rec = IdempotencyRecord.query.filter_by(
        actor_id=actor_id, method=request.method, path=request.path, key=key
    ).first()
    if rec is None or rec.expires_at <= now():
        return None
    if rec.status == "IN_PROGRESS":
        raise ApiError("DUPLICATE_REQUEST")
    if rec.request_hash != body_hash:
        raise ApiError("IDEMPOTENCY_KEY_CONFLICT")
    body = json.loads(rec.response_body or "{}")
    body.setdefault("meta", {})["replayed"] = True
    resp = jsonify(body)
    resp.status_code = rec.response_code or 200
    resp.headers["Idempotency-Replayed"] = "true"
    return resp


def register(key: str, body_hash: str) -> None:
    """事务内登记 IN_PROGRESS（U-6 首写者胜）；并发同键 → 409 DUPLICATE_REQUEST。"""
    from sqlalchemy.exc import IntegrityError

    ttl_hours = g.get("idempotency_ttl_hours", 24)
    record = IdempotencyRecord(
        actor_id=g.user.id, method=request.method, path=request.path,
        key=key, request_hash=body_hash, status="IN_PROGRESS",
        expires_at=now() + timedelta(hours=ttl_hours),
    )
    db.session.add(record)
    try:
        db.session.flush()
    except IntegrityError as exc:
        raise ApiError("DUPLICATE_REQUEST") from exc
    g.idempotency_record = record


def finish(status_code: int, envelope: dict) -> None:
    """业务成功后、提交前回写 DONE 与响应体（与业务写入同事务，§3.7）。"""
    record: IdempotencyRecord | None = g.get("idempotency_record")
    if record is None:
        return
    record.status = "DONE"
    record.response_code = status_code
    record.response_body = json.dumps(envelope, ensure_ascii=False)
