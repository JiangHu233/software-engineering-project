from __future__ import annotations

"""API-18a 用户概况 / 18b 账户启停（§4.18）。"""
from datetime import datetime

from flask import g, request

from ...core import idempotency
from ...core.auth import login_required
from ...core.db_txn import immediate_transaction
from ...core.errors import ApiError
from ...core.responses import ok
from ...extensions import db
from ...models import (ACTIVE_STATUSES, ROLE_ADM, RSV_BOOKED, RSV_CHECKED_IN,
                       RSV_COMPLETED, RSV_VIOLATED, RSV_CANCELLED, Reservation, User,
                       Violation, add_audit)
from ...schemas.common import field_str, get_json
from ...services import admin_resource, credit_service
from ...services.timeutil import iso
from .. import api_bp


@api_bp.get("/admin/users/<int:user_id>")
@login_required(roles=["ADM"])
def user_profile(user_id: int):
    user = db.session.get(User, user_id)
    if user is None:
        raise ApiError("NOT_FOUND")
    total = Reservation.query.filter(Reservation.user_id == user.id).count()
    booked = Reservation.query.filter(Reservation.user_id == user.id,
                                      Reservation.status == RSV_BOOKED).count()
    checked_in = Reservation.query.filter(Reservation.user_id == user.id,
                                          Reservation.status == RSV_CHECKED_IN).count()
    completed = Reservation.query.filter(Reservation.user_id == user.id,
                                         Reservation.status == RSV_COMPLETED).count()
    violated = Reservation.query.filter(Reservation.user_id == user.id,
                                        Reservation.status == RSV_VIOLATED).count()
    cancelled = Reservation.query.filter(Reservation.user_id == user.id,
                                         Reservation.status == RSV_CANCELLED).count()
    violations_total = Violation.query.filter(Violation.user_id == user.id).count()
    active_bl = credit_service.active_blacklist(user.id)
    # 字段白名单（§4.18.1）：禁 username/passwordHash
    return ok({
        "userId": user.id,
        "displayName": user.display_name,
        "role": user.role,
        "accountStatus": user.account_status,
        "createdAt": iso(user.created_at),
        "lastLoginAt": iso(user.last_login_at),
        "reservations": {
            "total": total, "booked": booked, "checkedIn": checked_in,
            "completed": completed, "violated": violated, "cancelled": cancelled,
        },
        "violations": {
            "termScore": credit_service.term_score(user.id),
            "total": violations_total,
        },
        "blacklist": {
            "active": active_bl is not None,
            "until": iso(active_bl.blacklist_until) if active_bl else None,
            "entriesInTerm": credit_service.entries_in_term(user.id),
        },
    })


@api_bp.patch("/admin/users/<int:user_id>")
@login_required(roles=["ADM"])
def update_account_status(user_id: int):
    key, body_hash = idempotency.require_key()
    replay = idempotency.precheck(g.user.id, key, body_hash)
    if replay is not None:
        return replay

    user = db.session.get(User, user_id)
    if user is None:
        raise ApiError("NOT_FOUND")

    body = get_json()
    account_status = body.get("accountStatus")
    if account_status not in ("ACTIVE", "DISABLED"):
        raise ApiError("VALIDATION_FAILED", fields={"accountStatus": ["只允许 ACTIVE/DISABLED"]})
    reason = field_str(body, "reason", max_len=200)
    if not reason:
        raise ApiError("REASON_REQUIRED")

    if account_status == "DISABLED" and user.account_status == "ACTIVE" and user.role == ROLE_ADM:
        active_admins = User.query.filter_by(role=ROLE_ADM, account_status="ACTIVE").count()
        if active_admins <= 1:  # 含"停用自己且自己是最后一个 ADM"（Q-23 裁决 C）
            raise ApiError("LAST_ADMIN_FORBIDDEN",
                           fields={"activeAdminCount": active_admins})

    with immediate_transaction():
        idempotency.register(key, body_hash)
        user.account_status = account_status
        affected = {"cancelled": 0, "ended": 0}
        if account_status == "DISABLED":
            # Q-24 裁决 B：取消未来 BOOKED + 终止 CHECKED_IN，均不记违约
            affected = admin_resource.disable_user_cascade(user.id)
        user.version += 1
        db.session.add(user)
        add_audit("USER_STATUS_CHANGE", target_type="user", target_id=user.id,
                  reason=reason, result_code=account_status)
        envelope = {"data": {
            "userId": user.id,
            "accountStatus": user.account_status,
            "reason": reason,
            "effectiveAt": iso(datetime.now()),
            "cancelledReservations": affected["cancelled"],
            "endedCheckIns": affected["ended"],
        }, "meta": {"requestId": getattr(g, "request_id", None)}}
        idempotency.finish(200, envelope)
    return ok(envelope["data"])
