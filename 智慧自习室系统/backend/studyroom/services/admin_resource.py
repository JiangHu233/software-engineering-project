from __future__ import annotations

"""管理端资源维护：房间/座位 CRUD + 停用联动（ST-02'/ST-06'）。

联动语义（Q-07/Q-12/Q-24/R-4）：
- MAINTENANCE：不动任何预约；
- DISABLED / 房间停用 / 账号停用：取消未来 BOOKED + 终止 CHECKED_IN
  （COMPLETED + terminatedBy，不记违约，violationsCreated 恒 0）。
"""
from ..extensions import db
from ..models import (ACTIVE_STATUSES, RSV_BOOKED, RSV_CHECKED_IN, Reservation,
                      SeatOccupancy, add_audit, add_outbox)
from ..core.errors import ApiError
from .timeutil import now


def cascade_terminate(reservations: list[Reservation], terminated_by: str,
                      audit_action: str) -> dict:
    """对给定预约执行停用联动。必须在 immediate_transaction 内调用。

    逐条条件更新（先到者胜）；每条：取消/终止 + 删全部占用 + outbox + 审计。
    返回 {cancelled, ended}。
    """
    t_now = now()
    cancelled = ended = 0
    for r in sorted(reservations, key=lambda x: x.id):  # 固定按 id 序（§5.4 L3*）
        if r.status == RSV_BOOKED and r.starts_at > t_now:
            updated = (db.session.query(Reservation)
                       .filter(Reservation.id == r.id, Reservation.status == RSV_BOOKED)
                       .update({"status": "CANCELLED", "cancelled_at": t_now,
                                "terminated_by": terminated_by}, synchronize_session=False))
            if updated:
                db.session.query(SeatOccupancy).filter(
                    SeatOccupancy.reservation_id == r.id).delete(synchronize_session=False)
                add_outbox("RESERVATION_CANCELLED", r.user_id, {"reservationId": r.id})
                add_audit(audit_action + "_CANCELLED", target_type="reservation",
                          target_id=r.id)
                cancelled += 1
        elif r.status == RSV_CHECKED_IN:
            updated = (db.session.query(Reservation)
                       .filter(Reservation.id == r.id, Reservation.status == RSV_CHECKED_IN)
                       .update({"status": "COMPLETED", "checked_out_at": t_now,
                                "terminated_by": terminated_by}, synchronize_session=False))
            if updated:
                db.session.query(SeatOccupancy).filter(
                    SeatOccupancy.reservation_id == r.id).delete(synchronize_session=False)
                add_outbox("SEAT_UNAVAILABLE", r.user_id, {"reservationId": r.id})
                add_audit(audit_action + "_TERMINATED", target_type="reservation",
                          target_id=r.id)
                ended += 1
    return {"cancelled": cancelled, "ended": ended}


def disable_seat_cascade(seat_id: int) -> dict:
    """座位 DISABLED 的联动（API-13d）。"""
    t_now = now()
    targets = Reservation.query.filter(
        Reservation.seat_id == seat_id, Reservation.status.in_(ACTIVE_STATUSES)).all()
    return cascade_terminate(targets, "SEAT_DISABLED", "SEAT_DISABLED")


def disable_room_cascade(seat_ids: list[int]) -> dict:
    """自习室停用的联动（API-13b）。"""
    t_now = now()
    targets = Reservation.query.filter(
        Reservation.seat_id.in_(seat_ids), Reservation.status.in_(ACTIVE_STATUSES)).all()
    return cascade_terminate(targets, "ROOM_DISABLED", "ROOM_DISABLED")


def disable_user_cascade(user_id: int) -> dict:
    """账号停用的联动（API-18b，Q-24 裁决 B）。"""
    targets = Reservation.query.filter(
        Reservation.user_id == user_id, Reservation.status.in_(ACTIVE_STATUSES)).all()
    return cascade_terminate(targets, "ACCOUNT_DISABLED", "ACCOUNT_DISABLED")
