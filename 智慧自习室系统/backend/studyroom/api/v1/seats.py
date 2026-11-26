from __future__ import annotations

"""API-04 座位状态查询（按日）/ API-05 单座位可用性（§4.4/4.5）。

学生端字段白名单在序列化层硬编码：响应结构中不存在占用者字段（PS-01/NFR-08）。
"""
from datetime import timedelta

from flask import g, request

from ...core.auth import login_required
from ...core.errors import ApiError, validation_failed
from ...core.pagination import parse_pagination
from ...core.responses import ok, page_meta
from ...extensions import db
from ...models import ROLE_OPS, ROLE_STU, Room, Seat, ZoneDictionary
from ...services import rule_params, seat_query
from ...services.timeutil import (combine, end_of_day, iso, iso_date, now, parse_date,
                                  parse_hhmm, today)
from .. import api_bp

STATE_ENUM = {"AVAILABLE", "BOOKED", "IN_USE", "MAINTENANCE", "DISABLED"}


def _parse_window(day, params):
    raw_from = request.args.get("from")
    raw_to = request.args.get("to")
    if (raw_from is None) != (raw_to is None):
        raise validation_failed({"from": ["from/to 必须成对出现"]})
    if raw_from is None:
        return combine(day, parse_hhmm("00:00", "from")), end_of_day(day)
    t_from = parse_hhmm(raw_from, "from")
    t_to = parse_hhmm(raw_to, "to")
    win_start, win_end = combine(day, t_from), combine(day, t_to)
    if win_start >= win_end:
        raise validation_failed({"from": ["from 必须早于 to"]})
    return win_start, win_end


@api_bp.get("/rooms/<int:room_id>/seats")
@login_required(roles=["STU", "ADM"])
def room_seats(room_id: int):
    if g.user.role == ROLE_OPS:
        raise ApiError("FORBIDDEN_ROLE")

    room = db.session.get(Room, room_id)
    if room is None:
        raise ApiError("NOT_FOUND")

    _, params = rule_params.current()
    raw_date = request.args.get("date")
    if not raw_date:
        raise validation_failed({"date": ["date 必填"]})
    day = parse_date(raw_date, "date")
    # 日期范围：今天 ~ 今天+可约天数（默认 7 天）
    max_day = today() + timedelta(days=int(params["bookingWindowDays"]))
    if day < today() or day > max_day:
        raise validation_failed({"date": [f"只能查询今天至 {max_day.isoformat()} 的日期"]})

    win_start, win_end = _parse_window(day, params)

    state_filter = request.args.get("state")
    states = None
    if state_filter:
        states = {s.strip().upper() for s in state_filter.split(",") if s.strip()}
        bad = states - STATE_ENUM
        if bad:
            raise validation_failed({"state": [f"不支持的取值：{','.join(bad)}"]})

    zone = request.args.get("zone")
    if zone:
        if not ZoneDictionary.query.filter_by(label=zone, active=True).first():
            raise validation_failed({"zone": ["zone 取值必须来自区域字典"]})

    feature = request.args.get("feature")

    page, page_size = parse_pagination()
    query = Seat.query.filter(Seat.room_id == room.id)
    if zone:
        query = query.filter(Seat.zone == zone)
    if feature:
        # 裁定#8：按"包含标签"精确匹配 JSON 数组元素，不用 LIKE 模糊
        query = query.filter(Seat.features_json.contains(f'"{feature}"'))
    seats = query.order_by(Seat.label.asc(), Seat.id.asc()).all()

    covering = seat_query.load_covering_reservations([s.id for s in seats], win_start, win_end)

    # state 为派生值：先全量计算再过滤，最后内存分页（保证 meta.total 与条目一致）
    all_items = []
    for seat in seats:
        state = seat_query.window_state(seat, covering.get(seat.id, []), win_start, win_end)
        if states and state not in states:
            continue
        # 字段白名单（§3.10）：不含任何占用者字段
        all_items.append({
            "seatId": seat.id,
            "seatLabel": seat.label,
            "zone": seat.zone,
            "features": seat.features,
            "state": state,
            "roomId": room.id,
            "roomName": room.name,
            "openHour": room.open_hour,
            "closeHour": room.close_hour,
        })

    total = len(all_items)
    items = all_items[(page - 1) * page_size: (page - 1) * page_size + page_size]
    meta = page_meta(page, page_size, total, asOf=iso(now()))
    return ok({"items": items}, meta=meta)


@api_bp.get("/seats/<int:seat_id>/availability")
@login_required(roles=["STU", "ADM"])
def seat_availability(seat_id: int):
    seat = db.session.get(Seat, seat_id)
    if seat is None:
        raise ApiError("NOT_FOUND")
    _, params = rule_params.current()
    raw_date = request.args.get("date")
    if not raw_date:
        raise validation_failed({"date": ["date 必填"]})
    day = parse_date(raw_date, "date")
    max_day = today() + timedelta(days=int(params["bookingWindowDays"]))
    if day < today() or day > max_day:
        raise validation_failed({"date": [f"只能查询今天至 {max_day.isoformat()} 的日期"]})

    slots = seat_query.day_slots(seat, seat.room, day, g.user.id)
    return ok({
        "seatId": seat.id,
        "seatLabel": seat.label,
        "date": iso_date(day),
        "slots": slots,
        "granularityMinutes": 60,
    })
