from __future__ import annotations

"""座位时段状态计算（C-7，§5.1.3）——API-04/05/06/14 共用同一判定函数。

红线：状态不落字段，实时按 (seat.state, 有效预约覆盖) 派生；
BOOKED 口径 = 存在覆盖该时刻的有效 BOOKED 预约（Q-27 裁决 B，无 15 分钟前置）。
"""
from datetime import date, datetime, time, timedelta

from ..extensions import db
from ..models import (ACTIVE_STATUSES, RSV_BOOKED, RSV_CHECKED_IN, Reservation,
                      SEAT_AVAILABLE, SEAT_DISABLED, SEAT_MAINTENANCE, Room, Seat)

# API-04 窗口聚合优先级（R-3）：数值越大越严重
_SEVERITY = {
    "AVAILABLE": 0, "BOOKED": 1, "IN_USE": 2, "MAINTENANCE": 3, "DISABLED": 4,
}


def load_covering_reservations(seat_ids: list[int], window_start: datetime,
                               window_end: datetime) -> dict[int, list[Reservation]]:
    """一次读取覆盖窗口的有效预约，按座位分组（同一读快照，§4.4）。"""
    rows = (Reservation.query
            .filter(Reservation.seat_id.in_(seat_ids),
                    Reservation.status.in_(ACTIVE_STATUSES),
                    Reservation.starts_at < window_end,
                    Reservation.ends_at > window_start)
            .all())
    by_seat: dict[int, list[Reservation]] = {}
    for r in rows:
        by_seat.setdefault(r.seat_id, []).append(r)
    return by_seat


def instant_state(seat: Seat, reservations: list[Reservation], t: datetime) -> str:
    """C-7 在时刻 t 的单值状态（§5.1.3 伪代码的可执行版）。"""
    if seat.state == SEAT_DISABLED:
        return "DISABLED"
    if seat.state == SEAT_MAINTENANCE:
        return "MAINTENANCE"
    for r in reservations:
        if r.status == RSV_CHECKED_IN and r.starts_at <= t < r.ends_at:
            return "IN_USE"
    for r in reservations:
        if r.status == RSV_BOOKED and r.starts_at <= t < r.ends_at:
            return "BOOKED"
    return "AVAILABLE"


def window_state(seat: Seat, reservations: list[Reservation],
                 window_start: datetime, window_end: datetime) -> str:
    """窗口内逐整点取最严重（R-3）。"""
    if seat.state in (SEAT_DISABLED, SEAT_MAINTENANCE):
        return seat.state
    worst = "AVAILABLE"
    t = window_start
    while t < window_end:
        state = instant_state(seat, reservations, t)
        if _SEVERITY[state] > _SEVERITY[worst]:
            worst = state
        t += timedelta(hours=1)
    return worst


def day_slots(seat: Seat, room: Room, day: date, viewer_id: int | None) -> list[dict]:
    """API-05：单座位逐小时 slots。枚举不含 MAINTENANCE，维护中座位按 SEAT_DISABLED 展示
    （契约枚举闭集；不可约语义一致，裁定#8）。"""
    from .timeutil import end_of_day, iso
    day_start = datetime.combine(day, time(0, 0))
    day_end = end_of_day(day)
    reservations = load_covering_reservations([seat.id], day_start, day_end).get(seat.id, [])
    slots = []
    for hour in range(24):
        t = day_start.replace(hour=hour)
        if not room.active:
            status = "ROOM_CLOSED"
        elif seat.state in (SEAT_DISABLED, SEAT_MAINTENANCE):
            status = "SEAT_DISABLED"
        else:
            status = instant_state(seat, reservations, t)
            if status == "MAINTENANCE":  # 逻辑上不会出现，防御
                status = "SEAT_DISABLED"
        reserved_by_me = any(
            r.user_id == viewer_id and r.starts_at <= t < r.ends_at
            for r in reservations if r.status in ACTIVE_STATUSES
        )
        slots.append({"hour": hour, "status": status, "reservedByMe": reserved_by_me})
    return slots
