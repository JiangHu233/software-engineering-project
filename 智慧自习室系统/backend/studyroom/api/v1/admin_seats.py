from __future__ import annotations

"""API-13c 新建座位 / 13d 修改座位（三态联动）（§4.13.3/4）。

路径按 §4.13.3 详细契约：POST /api/v1/admin/rooms/{roomId}/seats（裁定#1）。
"""
from flask import g, request

from ...core import idempotency
from ...core.auth import login_required
from ...core.db_txn import immediate_transaction
from ...core.errors import ApiError
from ...core.responses import created, ok
from ...extensions import db
from ...models import (Room, Seat, ZoneDictionary, SEAT_AVAILABLE, SEAT_DISABLED,
                       SEAT_MAINTENANCE, add_audit)
from ...schemas.common import field_str, field_str_list, get_json
from ...services import admin_resource
from .. import api_bp

SEAT_STATES = {SEAT_AVAILABLE, SEAT_MAINTENANCE, SEAT_DISABLED}


def _require_zone(zone: str) -> None:
    if not ZoneDictionary.query.filter_by(label=zone, active=True).first():
        raise ApiError("VALIDATION_FAILED",
                       fields={"zone": ["zone 取值必须来自区域字典"]})


@api_bp.post("/admin/rooms/<int:room_id>/seats")
@login_required(roles=["ADM"])
def create_seat(room_id: int):
    key, body_hash = idempotency.require_key()
    replay = idempotency.precheck(g.user.id, key, body_hash)
    if replay is not None:
        return replay

    room = db.session.get(Room, room_id)
    if room is None:
        raise ApiError("NOT_FOUND")
    if not room.active:
        raise ApiError("ROOM_INACTIVE", message="不允许向停用自习室添加座位。")

    body = get_json()
    seat_label = field_str(body, "seatLabel", max_len=20)
    zone = field_str(body, "zone", max_len=40)
    features = field_str_list(body, "features", max_items=8, item_max_len=20)
    _require_zone(zone)

    if Seat.query.filter(Seat.room_id == room.id, Seat.label == seat_label).first():
        raise ApiError("SEAT_LABEL_DUPLICATED")

    with immediate_transaction():
        idempotency.register(key, body_hash)
        seat = Seat(room_id=room.id, label=seat_label, zone=zone, state=SEAT_AVAILABLE)
        seat.features = features
        db.session.add(seat)
        db.session.flush()
        add_audit("SEAT_CREATED", target_type="seat", target_id=seat.id,
                  reason=f"{room.name}/{seat_label}")
        envelope = {"data": {
            "seatId": seat.id, "seatLabel": seat.label, "zone": seat.zone,
            "features": seat.features, "state": seat.state, "roomId": room.id,
        }, "meta": {"requestId": getattr(g, "request_id", None)}}
        idempotency.finish(201, envelope)
    return created(envelope["data"], f"/api/v1/admin/seats/{seat.id}")


@api_bp.patch("/admin/seats/<int:seat_id>")
@login_required(roles=["ADM"])
def update_seat(seat_id: int):
    key, body_hash = idempotency.require_key()
    replay = idempotency.precheck(g.user.id, key, body_hash)
    if replay is not None:
        return replay

    seat = db.session.get(Seat, seat_id)
    if seat is None:
        raise ApiError("NOT_FOUND")

    body = get_json()
    seat_label = field_str(body, "seatLabel", required=False, max_len=20)
    zone = field_str(body, "zone", required=False, max_len=40)
    features = body.get("features")
    state = body.get("state")
    reason = field_str(body, "reason", required=False, max_len=200)

    if state is not None:
        if state not in SEAT_STATES:
            raise ApiError("VALIDATION_FAILED", fields={"state": [f"state 非法：{state}"]})
        if state != SEAT_AVAILABLE and not reason:
            raise ApiError("REASON_REQUIRED", message="停用座位必须填写原因。")
    if zone is not None:
        _require_zone(zone)
    if features is not None and not isinstance(features, list):
        raise ApiError("VALIDATION_FAILED", fields={"features": ["features 必须为字符串数组"]})
    if seat_label and seat_label != seat.label:
        if Seat.query.filter(Seat.room_id == seat.room_id, Seat.label == seat_label).first():
            raise ApiError("SEAT_LABEL_DUPLICATED")

    with immediate_transaction():
        idempotency.register(key, body_hash)
        if seat_label:
            seat.label = seat_label
        if zone is not None:
            seat.zone = zone
        if isinstance(features, list):
            seat.features = [str(f) for f in features]
        affected = {"cancelled": 0, "ended": 0}
        if state is not None and state != seat.state:
            seat.state = state
            if state == SEAT_DISABLED:
                # Q-07 裁决 B / D-7：DISABLED 才联动；violationsCreated 恒 0（硬断言）
                affected = admin_resource.disable_seat_cascade(seat.id)
                add_audit("SEAT_DISABLED", target_type="seat", target_id=seat.id, reason=reason)
            elif state == SEAT_MAINTENANCE:
                # MAINTENANCE：不动任何预约（Q-07）
                add_audit("SEAT_MAINTENANCE", target_type="seat", target_id=seat.id, reason=reason)
        db.session.add(seat)
        envelope = {"data": {
            "seatId": seat.id, "seatState": seat.state,
            "cancelledReservations": affected["cancelled"],
            "endedCheckIns": affected["ended"],
            "violationsCreated": 0,
        }, "meta": {"requestId": getattr(g, "request_id", None)}}
        idempotency.finish(200, envelope)
    return ok(envelope["data"])
