from __future__ import annotations

import json
from datetime import datetime, timedelta

from flask import g, has_request_context

from ..extensions import db
from .base import TimestampMixin

OUTBOX_CREATED = "RESERVATION_CREATED"
OUTBOX_CANCELLED = "RESERVATION_CANCELLED"
OUTBOX_COMPLETED = "RESERVATION_COMPLETED"
OUTBOX_VIOLATION = "VIOLATION_CREATED"
OUTBOX_SEAT_UNAVAILABLE = "SEAT_UNAVAILABLE"


class IdempotencyRecord(db.Model):
    """幂等键登记（§3.7）。U-6：(actor, method, path, key) 唯一。"""
    __tablename__ = "idempotency_record"

    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, nullable=False)
    method = db.Column(db.String(10), nullable=False)
    path = db.Column(db.String(200), nullable=False)
    key = db.Column(db.String(64), nullable=False)
    request_hash = db.Column(db.String(64), nullable=False)
    status = db.Column(db.String(12), nullable=False, default="IN_PROGRESS")
    response_code = db.Column(db.Integer)
    response_body = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    expires_at = db.Column(db.DateTime, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("actor_id", "method", "path", "key", name="u6_idempotency"),
        db.Index("ix_idempotency_expires", "expires_at"),
    )


class AuditLog(db.Model):
    """审计（FR-20/NFR-06）：只增不改（F-10），应用层无 update/delete 路径。"""
    __tablename__ = "audit_log"

    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer)
    actor_role = db.Column(db.String(8))
    action = db.Column(db.String(40), nullable=False)
    target_type = db.Column(db.String(20))
    target_id = db.Column(db.Integer)
    # 原因只记摘要：长度 + 前 20 字符（§3.10）
    reason = db.Column(db.String(60))
    request_id = db.Column(db.String(40), index=True)
    rule_version = db.Column(db.Integer)
    result_code = db.Column(db.String(30))
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)

    __table_args__ = (
        db.Index("ix_audit_actor_created", "actor_id", "created_at"),
    )


class NotificationOutbox(db.Model):
    """transactional outbox（Q-13 裁决 B）：S1 写接口事务内插行，不投递；S2 补投递器。"""
    __tablename__ = "notification_outbox"

    id = db.Column(db.Integer, primary_key=True)
    event_type = db.Column(db.String(30), nullable=False)
    recipient_user_id = db.Column(db.Integer, nullable=False)
    payload_json = db.Column(db.Text, nullable=False, default="{}")
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    delivered_at = db.Column(db.DateTime)
    attempts = db.Column(db.Integer, nullable=False, default=0)

    __table_args__ = (
        db.Index("ix_outbox_recipient", "recipient_user_id", "delivered_at"),
    )


class TokenRevocation(db.Model):
    """登出吊销名单（Q-17 裁决 A）：保留到令牌 exp 之后可清理。"""
    __tablename__ = "token_revocation"

    jti = db.Column(db.String(40), primary_key=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    revoked_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    token_exp = db.Column(db.DateTime, nullable=False)


class ScanRun(db.Model):
    """定时任务运行记录（§5.6）：供 API-20 指标与失败告警（UC-14/NFR-05）。"""
    __tablename__ = "scan_run"

    id = db.Column(db.Integer, primary_key=True)
    task_name = db.Column(db.String(30), nullable=False)
    started_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    finished_at = db.Column(db.DateTime)
    duration_ms = db.Column(db.Integer)
    scanned = db.Column(db.Integer, nullable=False, default=0)
    succeeded = db.Column(db.Integer, nullable=False, default=0)
    failed = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(db.String(12), nullable=False, default="RUNNING")

    __table_args__ = (
        db.Index("ix_scan_run_task", "task_name", "started_at"),
    )


def add_outbox(event_type: str, recipient_user_id: int, payload: dict) -> None:
    db.session.add(NotificationOutbox(
        event_type=event_type,
        recipient_user_id=recipient_user_id,
        payload_json=json.dumps(payload, ensure_ascii=False),
    ))


def add_audit(action: str, *, target_type: str | None = None, target_id: int | None = None,
              reason: str | None = None, rule_version: int | None = None,
              result_code: str | None = None, actor_id: int | None = None,
              actor_role: str | None = None) -> None:
    """在业务事务内调用（同成同败，§5.7）；actor 缺省取当前请求用户。"""
    if has_request_context():
        user = getattr(g, "user", None)
        if user is not None:
            actor_id = actor_id if actor_id is not None else user.id
            actor_role = actor_role or user.role
        request_id = getattr(g, "request_id", None)
    else:  # 定时任务等无请求上下文
        request_id = None
    summary = None
    if reason:
        summary = f"len={len(reason)}:{reason[:20]}"
    db.session.add(AuditLog(
        actor_id=actor_id, actor_role=actor_role, action=action,
        target_type=target_type, target_id=target_id, reason=summary,
        request_id=request_id, rule_version=rule_version, result_code=result_code,
    ))


def idempotency_ttl(now: datetime, hours: int) -> datetime:
    return now + timedelta(hours=hours)
