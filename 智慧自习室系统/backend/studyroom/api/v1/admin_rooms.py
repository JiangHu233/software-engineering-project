from __future__ import annotations

"""API-13a 新建自习室 / 13b 修改自习室（停用联动）/ 14 实时看板（§4.13.1/2、§4.14）。"""
from flask import g, request

from ...core import idempotency
from ...core.auth import login_required
from ...core.db_txn import immediate_transaction
from ...core.errors import ApiError
from ...core.responses import created, ok
from ...extensions import db
from ...models import Room, Seat, add_audit
from ...schemas.common import field_int, field_str, get_json
from ...services import admin_resource, board_service
from ...services.timeutil import iso
from .. import api_bp


def _validate_schedule(open_hour: int, close_hour: int) -> None:
    if not (0 <= open_hour <= 23) or not (1 <= close_hour <= 24) or open_hour >= close_hour:
        raise ApiError("SCHEDULE_INVALID", fields={
            "openHour": [f"openHour={open_hour} 非法"],
            "closeHour": [f"closeHour={close_hour} 非法（须 > openHour，≤24）"],
        })


@api_bp.post("/admin/rooms")
@login_required(roles=["ADM"])
def create_room():
    key, body_hash = idempotency.require_key()
    replay = idempotency.precheck(g.user.id, key, body_hash)
    if replay is not None:
        return replay

    body = get_json()
    name = field_str(body, "name", max_len=80)
    location = field_str(body, "location", max_len=120)
    open_hour = field_int(body, "openHour", required=True)   # 必填无默认（Q-21 裁决 A）
    close_hour = field_int(body, "closeHour", required=True)
    _validate_schedule(open_hour, close_hour)

    if Room.query.filter(Room.name == name).first():
        raise ApiError("ROOM_NAME_DUPLICATED")

    with immediate_transaction():
        idempotency.register(key, body_hash)
        room = Room(name=name, location=location, open_hour=open_hour, close_hour=close_hour)
        db.session.add(room)
        db.session.flush()
        add_audit("ROOM_CREATED", target_type="room", target_id=room.id, reason=name)
        envelope = {"data": {
            "roomId": room.id, "name": room.name, "location": room.location,
            "openHour": room.open_hour, "closeHour": room.close_hour,
            "active": room.active, "seatCount": 0,
        }, "meta": {"requestId": getattr(g, "request_id", None)}}
        idempotency.finish(201, envelope)
    return created(envelope["data"], f"/api/v1/admin/rooms/{room.id}")


@api_bp.patch("/admin/rooms/<int:room_id>")
@login_required(roles=["ADM"])
def update_room(room_id: int):
    key, body_hash = idempotency.require_key()
    replay = idempotency.precheck(g.user.id, key, body_hash)
    if replay is not None:
        return replay

    room = db.session.get(Room, room_id)
    if room is None:
        raise ApiError("NOT_FOUND")

    body = get_json()
    name = field_str(body, "name", required=False, max_len=80)
    location = field_str(body, "location", required=False, max_len=120)
    open_hour = field_int(body, "openHour", required=False)
    close_hour = field_int(body, "closeHour", required=False)
    active = body.get("active")
    reason = field_str(body, "reason", required=False, max_len=200)

    if active is not None and not isinstance(active, bool):
        raise ApiError("VALIDATION_FAILED", fields={"active": ["active 必须为布尔值"]})
    if active is False and not reason:
        raise ApiError("REASON_REQUIRED", message="停用自习室必须填写原因。")
    new_open = open_hour if open_hour is not None else room.open_hour
    new_close = close_hour if close_hour is not None else room.close_hour
    _validate_schedule(new_open, new_close)
    if name and name != room.name and Room.query.filter(Room.name == name).first():
        raise ApiError("ROOM_NAME_DUPLICATED")

    with immediate_transaction():
        idempotency.register(key, body_hash)
        if name:
            room.name = name
        if location:
            room.location = location
        room.open_hour, room.close_hour = new_open, new_close
        affected = {"cancelled": 0, "ended": 0}
        if active is not None and active != room.active:
            room.active = active
            if not active:
                # 停用联动（ST-02'/ST-06'）：取消未来 BOOKED + 终止 CHECKED_IN，不记违约
                seat_ids = [s.id for s in Seat.query.filter(
                    Seat.room_id == room.id).with_entities(Seat.id).all()]
                affected = admin_resource.disable_room_cascade(seat_ids)
                add_audit("ROOM_DISABLED", target_type="room", target_id=room.id, reason=reason)
        room.version += 1
        db.session.add(room)
        envelope = {"data": {
            "roomId": room.id, "name": room.name, "location": room.location,
            "openHour": room.open_hour, "closeHour": room.close_hour,
            "active": room.active,
            "affectedReservations": affected["cancelled"],
            "endedCheckIns": affected["ended"],
        }, "meta": {"requestId": getattr(g, "request_id", None)}}
        idempotency.finish(200, envelope)
    return ok(envelope["data"])


@api_bp.get("/admin/rooms/<int:room_id>/board")
@login_required(roles=["ADM"])
def room_board(room_id: int):
    data = board_service.board(room_id)
    if data is None:
        raise ApiError("NOT_FOUND")
    return ok(data)
