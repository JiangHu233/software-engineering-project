from __future__ import annotations

"""API-01 登录 / API-02 登出 / API-03 当前会话（§4.1~4.3）。"""
from datetime import datetime

from flask import g, request

from ...core.auth import (current_token_payload, issue_token, login_required,
                          revoke_token)
from ...core.db_txn import immediate_transaction
from ...core.errors import ApiError
from ...core.responses import ok
from ...extensions import db
from ...models import User, add_audit
from ...schemas.common import field_str, get_json
from ...services import credit_service, rule_params
from ...services.timeutil import iso
from .. import api_bp


def _user_payload(user: User) -> dict:
    return {
        "userId": user.id,
        "username": user.username,
        "displayName": user.display_name,
        "role": user.role,
        "accountStatus": user.account_status,
    }


def _blacklist_payload(user_id: int) -> dict:
    active = credit_service.active_blacklist(user_id)
    return {
        "active": active is not None,
        "until": iso(active.blacklist_until) if active else None,
        "reason": None,
        "entriesInTerm": credit_service.entries_in_term(user_id),
        "violationScore": credit_service.term_score(user_id),
    }


@api_bp.post("/auth/login")
def login():
    body = get_json()
    username = field_str(body, "username", max_len=40)
    password_raw = body.get("password")
    if not isinstance(password_raw, str) or not (8 <= len(password_raw) <= 72):
        raise ApiError("VALIDATION_FAILED", fields={
            "password": ["password 必填且长度 8~72 字符（超长不截断）"]})

    user = User.query.filter_by(username=username).first()
    if user is None or not user.check_password(password_raw):
        # 防枚举：不区分"用户不存在"与"密码错误"（§4.1）
        add_audit_login_fail(username)
        raise ApiError("UNAUTHENTICATED", message="账号或密码不正确。")
    if not user.is_active_account:
        add_audit_login_fail(username, code="ACCOUNT_DISABLED", user_id=user.id)
        raise ApiError("ACCOUNT_DISABLED")

    _, params = rule_params.current()
    token, exp = issue_token(user, int(params["tokenTtlHours"]))
    with immediate_transaction():
        user.last_login_at = datetime.now()
        db.session.add(user)
        add_audit("LOGIN_SUCCESS", target_type="user", target_id=user.id)
    return ok({
        "token": token,
        "expiresAt": iso(exp),
        "user": _user_payload(user),
        "blacklist": _blacklist_payload(user.id),
    })


def add_audit_login_fail(username: str, code: str = "UNAUTHENTICATED",
                         user_id: int | None = None) -> None:
    """登录失败审计：只记用户名与 IP，不记口令（§4.1）。"""
    try:
        with immediate_transaction():
            add_audit("LOGIN_FAILED", target_type="user", target_id=user_id,
                      reason=f"u={username};ip={request.remote_addr}", result_code=code)
    except Exception:
        pass


@api_bp.post("/auth/logout")
@login_required()
def logout():
    payload = current_token_payload()
    with immediate_transaction():
        revoke_token(payload, g.user.id)
        add_audit("LOGOUT", target_type="user", target_id=g.user.id)
    return ok({"loggedOut": True})


@api_bp.get("/auth/me")
@login_required()
def me():
    # active 按 blacklistUntil > now() 实时计算（ST-14a 兜底）
    return ok({"user": _user_payload(g.user), "blacklist": _blacklist_payload(g.user.id)})
