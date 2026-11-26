from __future__ import annotations

from ..extensions import db
from .base import TimestampMixin

RSV_BOOKED = "BOOKED"
RSV_CHECKED_IN = "CHECKED_IN"
RSV_COMPLETED = "COMPLETED"
RSV_CANCELLED = "CANCELLED"
RSV_VIOLATED = "VIOLATED"
ACTIVE_STATUSES = (RSV_BOOKED, RSV_CHECKED_IN)

TERMINATED_BY_SEAT = "SEAT_DISABLED"
TERMINATED_BY_ROOM = "ROOM_DISABLED"
TERMINATED_BY_ACCOUNT = "ACCOUNT_DISABLED"


class Reservation(db.Model, TimestampMixin):
    __tablename__ = "reservation"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    seat_id = db.Column(db.Integer, db.ForeignKey("seat.id"), nullable=False)
    starts_at = db.Column(db.DateTime, nullable=False)
    ends_at = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(12), nullable=False, default=RSV_BOOKED)
    checked_in_at = db.Column(db.DateTime)
    checked_out_at = db.Column(db.DateTime)
    cancelled_at = db.Column(db.DateTime)
    # Q-12 裁决 A：系统终止的 COMPLETED 记录落此标记，不引入新状态；完成率统计须排除非空值
    terminated_by = db.Column(db.String(20))
    cancel_reason = db.Column(db.String(200))
    # S2 代约预留（§5.10），S1 一律 SELF
    operator_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    operator_reason = db.Column(db.String(200))
    created_via = db.Column(db.String(8), nullable=False, default="SELF")

    user = db.relationship("User", foreign_keys=[user_id], backref="reservations")
    seat = db.relationship("Seat", backref="reservations")
    operator = db.relationship("User", foreign_keys=[operator_id])

    __table_args__ = (
        db.Index("ix_reservation_user_status_starts", "user_id", "status", "starts_at"),
        db.Index("ix_reservation_status_ends", "status", "ends_at"),
        db.Index("ix_reservation_seat_status", "seat_id", "status"),
    )


class SeatOccupancy(db.Model):
    """座位 × 整点时段占用（ADR-002，方案 A：只存当前有效占用，释放即删行）。"""
    __tablename__ = "seat_occupancy"

    id = db.Column(db.Integer, primary_key=True)
    seat_id = db.Column(db.Integer, db.ForeignKey("seat.id"), nullable=False)
    reservation_id = db.Column(db.Integer, db.ForeignKey("reservation.id"), nullable=False, index=True)
    slot_start = db.Column(db.DateTime, nullable=False)

    # U-1：同一座位同一整点至多一条有效占用（BR-04/FR-13 的数据库兜底）
    __table_args__ = (
        db.UniqueConstraint("seat_id", "slot_start", name="u1_occupancy"),
    )
