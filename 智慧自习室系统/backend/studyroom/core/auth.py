from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from functools import wraps

import jwt
from flask import current_app, g, request

from ..extensions import db
from ..models import TokenRevocation, User
from ..models.system import add_audit
from .errors import ApiError


def issue_token(user: User, ttl_hours: int) -> tuple[str, datetime]:
    """签发 JWT（Q-17 裁决 A）：载荷仅 sub/role/jti/iat/exp，8h，多会话各自独立 jti。"""
    now = datetime.now()
    exp = now + timedelta(hours=ttl_hours)
    payload = {
        "sub": str(user.id),  # RFC 7519：sub 必须为字符串（PyJWT 2.10+ 强制校验）
        "role": user.role,
        "jti": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    token = jwt.encode(payload, current_app.config["SECRET_KEY"], algorithm="HS256")
    return token, exp


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, current_app.config["SECRET_KEY"], algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise ApiError("UNAUTHENTICATED") from exc


def _extract_bearer() -> dict:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise ApiError("UNAUTHENTICATED")
    return decode_token(header[7:].strip())


def current_token_payload() -> dict:
    return _extract_bearer()


def login_required(roles: list[str] | None = None):
    """五步判定的前三步（§3.3，顺序是契约的一部分）：
    ① 令牌有效未吊销 → ② 账号 ACTIVE → ③ 角色；
    ④ 对象归属由各视图自行判定（403 FORBIDDEN_OBJECT，禁止 404 掩盖）。
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            payload = _extract_bearer()
            jti = payload.get("jti")
            if db.session.get(TokenRevocation, jti):
                raise ApiError("UNAUTHENTICATED")
            user = db.session.get(User, int(payload["sub"]))
            if user is None:
                raise ApiError("UNAUTHENTICATED")
            if not user.is_active_account:
                add_audit_standalone("ACCOUNT_DISABLED_REJECT", target_type="user",
                                     target_id=user.id, result_code="ACCOUNT_DISABLED")
                raise ApiError("ACCOUNT_DISABLED")
            if roles is not None and user.role not in roles:
                add_audit_standalone("FORBIDDEN_ROLE", target_type="user",
                                     target_id=user.id, result_code="FORBIDDEN_ROLE")
                raise ApiError("FORBIDDEN_ROLE")
            g.user = user
            g.jti = jti
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def add_audit_standalone(action: str, **kwargs) -> None:
    """鉴权失败等场景：审计独立提交（请求事务尚未开始）。"""
    from .db_txn import immediate_transaction
    try:
        with immediate_transaction():
            add_audit(action, **kwargs)
    except Exception:  # 审计失败不改变鉴权结论
        current_app.logger.exception("standalone audit failed: %s", action)


def revoke_token(payload: dict, user_id: int) -> None:
    exp = datetime.fromtimestamp(payload["exp"])
    db.session.add(TokenRevocation(jti=payload["jti"], user_id=user_id, token_exp=exp))
