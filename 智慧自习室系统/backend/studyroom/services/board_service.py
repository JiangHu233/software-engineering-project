from __future__ import annotations

"""看板与使用中列表（API-14/15）：C-7 实时计算，不读缓存字段。"""
from datetime import timedelta

from ..extensions import db
from ..models import RSV_CHECKED_IN, Reservation, Room, Seat
from . import seat_query
from .timeutil import now


def board(room_id: int) -> dict | None:
    room = db.session.get(Room, room_id)
    if room is None:
        return None
    t_now = now()
    seats = Seat.query.filter(Seat.room_id == room.id).order_by(Seat.label, Seat.id).all()
    covering = seat_query.load_covering_reservations(
        [s.id for s in seats], t_now, t_now + timedelta(seconds=1))
    summary = {"available": 0, "booked": 0, "inUse": 0, "maintenance": 0, "disabled": 0}
    seat_items = []
    for seat in seats:
        state = seat_query.instant_state(seat, covering.get(seat.id, []), t_now)
        summary[state.lower() if state != "IN_USE" else "inUse"] += 1
        current = None
        if state in ("IN_USE", "BOOKED"):
            for r in covering.get(seat.id, []):
                if r.starts_at <= t_now < r.ends_at:
                    current = r
                    if state == "IN_USE":
                        break
        item = {
            "seatId": seat.id, "seatLabel": seat.label, "state": state,
        }
        if current is not None:
            item["currentReservation"] = {
                "reservationId": current.id,
                "userDisplayName": current.user.display_name,
                "startsAt": current.starts_at.isoformat(),
                "endsAt": current.ends_at.isoformat(),
                "checkedInAt": current.checked_in_at.isoformat() if current.checked_in_at else None,
                "violationRegisterable": state == "IN_USE" and current.status == RSV_CHECKED_IN,
            }
        else:
            item["currentReservation"] = None
        seat_items.append(item)
    return {"roomId": room.id, "roomName": room.name,
            "asOf": t_now.isoformat(timespec="seconds"),
            "summary": summary, "seats": seat_items}


def in_use_list(filters: dict) -> list[dict]:
    t_now = now()
    query = (Reservation.query
             .join(Seat, Reservation.seat_id == Seat.id)
             .join(Room, Seat.room_id == Room.id)
             .filter(Reservation.status == RSV_CHECKED_IN,
                     Reservation.ends_at > t_now))
    if filters.get("roomId"):
        query = query.filter(Seat.room_id == filters["roomId"])
    if filters.get("seatId"):
        query = query.filter(Reservation.seat_id == filters["seatId"])
    query = query.order_by(Reservation.ends_at.asc(), Reservation.id.asc())
    items = []
    for r in query.all():
        items.append({
            "reservationId": r.id,
            "roomId": r.seat.room_id, "roomName": r.seat.room.name,
            "seatId": r.seat_id, "seatLabel": r.seat.label,
            "userDisplayName": r.user.display_name,
            "startsAt": r.starts_at.isoformat(),
            "endsAt": r.ends_at.isoformat(),
            "checkedInAt": r.checked_in_at.isoformat() if r.checked_in_at else None,
            "minutesUntilEnd": max(0, int((r.ends_at - t_now).total_seconds() // 60)),
            "violationRegisterable": True,
        })
    return items
