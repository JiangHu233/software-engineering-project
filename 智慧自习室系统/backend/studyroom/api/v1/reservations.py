from __future__ import annotations

"""API-06 创建 / 07 取消 / 08 签到 / 09 签退 / 11 详情（§4.6~4.9、§4.11）——核心写接口。"""
from datetime import timedelta

from flask import g, request

from ...core import idempotency
from ...core.auth import login_required
from ...core.db_txn import immediate_transaction
from ...core.errors import ApiError
from ...core.responses import created, ok
from ...extensions import db
from ...models import (RSV_BOOKED, ROLE_STU, Reservation, Violation,
                       add_audit)
from ...schemas.common import field_str, get_json
from ...services import reservation_service, rule_params
from ...services.timeutil import iso, now, parse_iso_local
from ...services.violation_service import MASKED_TEMPLATES
from .. import api_bp


def _get_reservation_or_404(reservation_id: int) -> Reservation:
    reservation = db.session.get(Reservation, reservation_id)
    if reservation is None:
        raise ApiError("NOT_FOUND")
    return reservation


def _reservation_payload(reservation: Reservation, *, include_eligibility: bool = True,
                         include_seat: bool = True) -> dict:
    data = {
        "reservationId": reservation.id,
        "status": reservation.status,
        "startsAt": iso(reservation.starts_at),
        "endsAt": iso(reservation.ends_at),
        "checkedInAt": iso(reservation.checked_in_at),
        "checkedOutAt": iso(reservation.checked_out_at),
        "cancelledAt": iso(reservation.cancelled_at),
        "terminatedBy": reservation.terminated_by,
        "createdAt": iso(reservation.created_at),
    }
    if include_seat:
        seat = reservation.seat
        data.update({
            "seatId": seat.id, "seatLabel": seat.label,
            "roomId": seat.room_id, "roomName": seat.room.name,
        })
    if include_eligibility:
        data.update(reservation_service.eligibility(reservation))
    return data


@api_bp.post("/reservations")
@login_required(roles=["STU"])
def create_reservation():
    key, body_hash = idempotency.require_key()
    replay = idempotency.precheck(g.user.id, key, body_hash)
    if replay is not None:
        return replay

    body = get_json()
    seat_id = body.get("seatId")
    if seat_id is None:
        raise ApiError("VALIDATION_FAILED", fields={"seatId": ["seatId 必填"]})
    starts_raw, ends_raw = body.get("startsAt"), body.get("endsAt")
    if not starts_raw or not ends_raw:
        raise ApiError("VALIDATION_FAILED",
                       fields={k: ["必填"] for k in ("startsAt", "endsAt") if not body.get(k)})
    starts_at = parse_iso_local(starts_raw, "startsAt")
    ends_at = parse_iso_local(ends_raw, "endsAt")

    # 锁外快检（§5.2 步骤 5~10：黑名单/R2/R8/座位/房间/R-open）
    seat, params = reservation_service.precheck_create(g.user, seat_id, starts_at, ends_at)

    with immediate_transaction():
        idempotency.register(key, body_hash)               # L4
        reservation = reservation_service.create_locked(   # L1 → L2 → 写入
            g.user, seat, starts_at, ends_at, params)
        _, params_now = rule_params.current()
        window_end = starts_at + timedelta(minutes=int(params_now["checkInWindowMinutes"]))
        envelope = {"data": {
            "reservationId": reservation.id,
            "status": reservation.status,
            "seatId": seat.id, "seatLabel": seat.label,
            "roomId": seat.room_id, "roomName": seat.room.name,
            "startsAt": iso(starts_at), "endsAt": iso(ends_at),
            "checkInWindow": {"start": iso(starts_at), "end": iso(window_end)},
            "activeReservationCount": reservation_service.active_count_of(g.user.id),
            "createdAt": iso(reservation.created_at),
        }, "meta": {"requestId": getattr(g, "request_id", None)}}
        idempotency.finish(201, envelope)
    return created(envelope["data"], f"/api/v1/reservations/{reservation.id}")


@api_bp.post("/reservations/<int:reservation_id>/cancel")
@login_required(roles=["STU"])
def cancel_reservation(reservation_id: int):
    key, body_hash = idempotency.require_key()
    replay = idempotency.precheck(g.user.id, key, body_hash)
    if replay is not None:
        return replay

    body = get_json()
    reason = field_str(body, "reason", required=False, max_len=200)

    reservation = _get_reservation_or_404(reservation_id)
    if reservation.user_id != g.user.id:  # 对象归属（步骤 4，不得用 404 掩盖）
        raise ApiError("FORBIDDEN_OBJECT")

    _, params = rule_params.current()
    t_now = now()
    if reservation.status != RSV_BOOKED:
        raise ApiError("RESERVATION_STATE_CONFLICT",
                       fields={"currentStatus": reservation.status})
    if not (t_now <= reservation.starts_at - timedelta(
            minutes=int(params["cancelLeadMinutes"]))):  # 含等于（Q-03 裁决 C）
        raise ApiError("CANCEL_TOO_LATE", fields={
            "cancelDeadline": iso(reservation.starts_at - timedelta(
                minutes=int(params["cancelLeadMinutes"])))})

    with immediate_transaction():
        idempotency.register(key, body_hash)
        reservation_service.cancel_locked(reservation, reason)
        envelope = {"data": {
            "reservationId": reservation.id,
            "status": "CANCELLED",
            "cancelledAt": iso(reservation.cancelled_at),
            "seatFreed": True,
            "activeReservationCount": reservation_service.active_count_of(g.user.id),
        }, "meta": {"requestId": getattr(g, "request_id", None)}}
        idempotency.finish(200, envelope)
    return ok(envelope["data"])


@api_bp.post("/reservations/<int:reservation_id>/check-in")
@login_required(roles=["STU"])
def check_in(reservation_id: int):
    key, body_hash = idempotency.require_key()
    replay = idempotency.precheck(g.user.id, key, body_hash)
    if replay is not None:
        return replay

    reservation = _get_reservation_or_404(reservation_id)
    if reservation.user_id != g.user.id:
        raise ApiError("FORBIDDEN_OBJECT")
    if reservation.status not in (RSV_BOOKED, "CHECKED_IN"):
        raise ApiError("RESERVATION_STATE_CONFLICT",
                       fields={"currentStatus": reservation.status})

    with immediate_transaction():
        idempotency.register(key, body_hash)
        result = reservation_service.check_in_locked(reservation)
        envelope = {"data": {
            "reservationId": reservation.id,
            "status": reservation.status,
            "checkedInAt": iso(reservation.checked_in_at),
            "seatState": "IN_USE",
            "checkOutDeadline": iso(result["endsAt"]),
        }, "meta": {"requestId": getattr(g, "request_id", None)}}
        idempotency.finish(200, envelope)
    return ok(envelope["data"])


@api_bp.post("/reservations/<int:reservation_id>/check-out")
@login_required(roles=["STU"])
def check_out(reservation_id: int):
    key, body_hash = idempotency.require_key()
    replay = idempotency.precheck(g.user.id, key, body_hash)
    if replay is not None:
        return replay

    reservation = _get_reservation_or_404(reservation_id)
    if reservation.user_id != g.user.id:
        raise ApiError("FORBIDDEN_OBJECT")
    if reservation.status != "CHECKED_IN":
        raise ApiError("RESERVATION_STATE_CONFLICT",
                       fields={"currentStatus": reservation.status})

    with immediate_transaction():
        idempotency.register(key, body_hash)
        result = reservation_service.check_out_locked(reservation)
        envelope = {"data": {
            "reservationId": reservation.id,
            "status": reservation.status,
            "checkedOutAt": iso(reservation.checked_out_at),
            "early": result["early"],
            "violationCreated": result["violationCreated"],
            "violationId": result["violationId"],
            "seatFreedFrom": iso(result["seatFreedFrom"]),
        }, "meta": {"requestId": getattr(g, "request_id", None)}}
        idempotency.finish(200, envelope)
    return ok(envelope["data"])


@api_bp.get("/reservations/<int:reservation_id>")
@login_required()
def reservation_detail(reservation_id: int):
    reservation = _get_reservation_or_404(reservation_id)
    is_owner = reservation.user_id == g.user.id
    is_admin = g.user.role == "ADM"
    if not is_owner and not is_admin:
        raise ApiError("FORBIDDEN_OBJECT")

    include_original = request.args.get("includeOriginalReason", "").lower() in ("1", "true", "yes")
    if include_original:
        with immediate_transaction():
            add_audit("VIEW_ORIGINAL_REASON", target_type="reservation",
                      target_id=reservation.id)

    data = _reservation_payload(reservation)
    violations = Violation.query.filter(
        Violation.reservation_id == reservation.id).order_by(Violation.id).all()
    v_items = []
    for v in violations:
        item = {
            "violationId": v.id,
            "kind": v.kind,
            "createdAt": iso(v.created_at),
            # 学生默认脱敏模板；ADM 直接原文（Q-20 裁决 C）
            "reason": v.reason if is_admin else MASKED_TEMPLATES.get(v.kind, v.reason),
        }
        if is_admin:
            item["operatorDisplayName"] = v.operator.display_name if v.operator else None
        if include_original:
            item["reasonOriginal"] = v.reason
        v_items.append(item)
    data["violations"] = v_items
    return ok(data)
