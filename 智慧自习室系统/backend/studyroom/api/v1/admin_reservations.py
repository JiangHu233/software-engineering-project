from __future__ import annotations

"""API-15 使用中列表 / 16 手动违约登记 / 17 预约检索（§4.15~4.17）。"""
from flask import g, request

from ...core import idempotency
from ...core.auth import login_required
from ...core.db_txn import immediate_transaction
from ...core.errors import ApiError
from ...core.pagination import apply_sort, parse_enum_list, parse_from_to, parse_pagination
from ...core.responses import ok, page_meta, created
from ...extensions import db
from ...models import Reservation, Seat, User, Violation, VK_ON_SITE, add_audit
from ...schemas.common import field_str, get_json
from ...services import board_service, violation_service
from ...services.timeutil import iso
from .. import api_bp

RSV_STATUSES = {"BOOKED", "CHECKED_IN", "COMPLETED", "CANCELLED", "VIOLATED"}

_SORT_FIELDS = {
    "startsAt": Reservation.starts_at,
    "endsAt": Reservation.ends_at,
    "status": Reservation.status,
    "createdAt": Reservation.created_at,
}


@api_bp.get("/admin/reservations/in-use")
@login_required(roles=["ADM"])
def in_use():
    page, page_size = parse_pagination()
    room_id = request.args.get("roomId", type=int)
    seat_id = request.args.get("seatId", type=int)
    all_items = board_service.in_use_list({"roomId": room_id, "seatId": seat_id})
    total = len(all_items)
    items = all_items[(page - 1) * page_size: (page - 1) * page_size + page_size]
    return ok({"items": items}, meta=page_meta(page, page_size, total))


@api_bp.post("/admin/reservations/<int:reservation_id>/violations")
@login_required(roles=["ADM"])
def register_violation(reservation_id: int):
    key, body_hash = idempotency.require_key()
    replay = idempotency.precheck(g.user.id, key, body_hash)
    if replay is not None:
        return replay

    body = get_json()
    kind = body.get("kind")
    if kind != VK_ON_SITE:  # Q-22 裁决 A：唯一合法值
        raise ApiError("VALIDATION_FAILED",
                       fields={"kind": ["kind 只允许 ON_SITE，细分情况写入 reason"]})
    reason = field_str(body, "reason", max_len=300)
    if not reason or not reason.strip():
        raise ApiError("REASON_REQUIRED")
    evidence = field_str(body, "evidence", required=False, max_len=500)

    reservation = db.session.get(Reservation, reservation_id)
    if reservation is None:
        raise ApiError("NOT_FOUND")
    if reservation.status != "CHECKED_IN":  # BR-11：仅"已签到"
        # TC-API16-07：失败登记同样留痕（"1 成功 1 失败"审计），先落库再拒绝。
        with immediate_transaction():
            add_audit("VIOLATION_REJECTED", target_type="reservation",
                      target_id=reservation.id,
                      result_code="RESERVATION_STATE_CONFLICT")
        raise ApiError("RESERVATION_STATE_CONFLICT",
                       message="仅能对正在使用中的预约登记违约。",
                       fields={"currentStatus": reservation.status})

    with immediate_transaction():
        idempotency.register(key, body_hash)
        outcome = violation_service.record_violation(
            reservation, VK_ON_SITE, operator_id=g.user.id, reason=reason,
            evidence=evidence, on_duplicate="raise",
            audit_action="VIOLATION_MANUAL")
        assert outcome is not None
        envelope = {"data": {
            "violationId": outcome.violation.id,
            "reservationId": reservation.id,
            "kind": VK_ON_SITE,
            "reason": outcome.violation.reason,
            "operatorDisplayName": g.user.display_name,
            "createdAt": iso(outcome.violation.created_at),
            "reservationStatus": "VIOLATED",
            "termViolationScore": outcome.score,
            "blacklistTriggered": outcome.blacklist_triggered,
            "blacklistUntil": iso(outcome.blacklist_until),
        }, "meta": {"requestId": getattr(g, "request_id", None)}}
        idempotency.finish(201, envelope)
    return created(envelope["data"],
                   f"/api/v1/admin/reservations/{reservation.id}/violations/{outcome.violation.id}")


@api_bp.get("/admin/reservations")
@login_required(roles=["ADM"])
def search_reservations():
    page, page_size = parse_pagination()
    dt_from, dt_to = parse_from_to()
    statuses = parse_enum_list("status", RSV_STATUSES)
    user_id = request.args.get("userId", type=int)
    seat_id = request.args.get("seatId", type=int)
    room_id = request.args.get("roomId", type=int)
    keyword = request.args.get("keyword")

    query = (Reservation.query
             .join(User, Reservation.user_id == User.id)
             .join(Seat, Reservation.seat_id == Seat.id))
    if user_id:
        query = query.filter(Reservation.user_id == user_id)
    if seat_id:
        query = query.filter(Reservation.seat_id == seat_id)
    if room_id:
        query = query.filter(Seat.room_id == room_id)
    if statuses:
        query = query.filter(Reservation.status.in_(statuses))
    if dt_from and dt_to:
        query = query.filter(Reservation.starts_at >= dt_from, Reservation.starts_at < dt_to)
    if keyword:
        # 前缀匹配 displayName，不支持学号模糊（NFR-08）
        query = query.filter(User.display_name.startswith(keyword))
    query = apply_sort(query, _SORT_FIELDS,
                       [(Reservation.starts_at, True), (Reservation.id, True)],
                       request.args.get("sort"))
    total = query.count()
    rows = query.offset((page - 1) * page_size).limit(page_size).all()

    violation_ids = {v.reservation_id for v in Violation.query.with_entities(
        Violation.reservation_id).all()}
    items = [{
        "reservationId": r.id,
        "userId": r.user_id,
        "userDisplayName": r.user.display_name,
        "roomName": r.seat.room.name,
        "seatLabel": r.seat.label,
        "startsAt": iso(r.starts_at), "endsAt": iso(r.ends_at),
        "status": r.status,
        "checkedInAt": iso(r.checked_in_at), "checkedOutAt": iso(r.checked_out_at),
        "hasViolation": r.id in violation_ids,
    } for r in rows]
    return ok({"items": items}, meta=page_meta(page, page_size, total))
