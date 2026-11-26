from __future__ import annotations

"""预约核心域（API-06/07/08/09 + eligibility）。

校验链顺序（§5.2，命中即停不可调整）：
锁外快检：格式 → 黑名单 → R2 过去 → R8 窗口 → 座位生命周期 → 房间停用 → R-open 开放时间；
锁内复核（权威判定）：L4 幂等 → L1 配额/本人重叠 → L2 座位占用 → 写入。
"""
from datetime import timedelta

from ..extensions import db
from ..models import (ACTIVE_STATUSES, RSV_BOOKED, RSV_CHECKED_IN, Reservation,
                      Seat, SeatOccupancy, SEAT_AVAILABLE, add_audit, add_outbox)
from ..core.errors import ApiError, validation_failed
from . import rule_params, seat_query
from .timeutil import effective_end_hour, is_on_hour, iso, now, slot_range


def check_on_hour(starts_at, ends_at) -> None:
    fields = {}
    if not is_on_hour(starts_at):
        fields["startsAt"] = ["必须为整点"]
    if not is_on_hour(ends_at):
        fields["endsAt"] = ["必须为整点"]
    if fields:
        raise validation_failed(fields)
    if ends_at <= starts_at:
        raise validation_failed({"endsAt": ["endsAt 必须严格大于 startsAt"]})


def precheck_create(user, seat_id: int, starts_at, ends_at) -> tuple[object, dict]:
    """锁外快检（只读，快速失败）。返回 (seat, params)。"""
    _, params = rule_params.current()
    check_on_hour(starts_at, ends_at)

    # 黑名单（决策表 R1）
    from . import credit_service
    active_bl = credit_service.active_blacklist(user.id)
    if active_bl is not None:
        raise ApiError("BLACKLISTED", fields={
            "blacklistUntil": iso(active_bl.blacklist_until),
            "violationScore": credit_service.term_score(user.id),
        })

    t_now = now()
    # R2 过去
    if starts_at < t_now:
        raise ApiError("RESERVATION_BLOCKED", fields={"rule": "R2", "ruleId": "F-1"})
    # R8 可约窗口
    allowed_until = t_now + timedelta(days=int(params["bookingWindowDays"]))
    if starts_at > allowed_until:
        raise ApiError("RESERVATION_BLOCKED", fields={
            "rule": "R8", "ruleId": "P-2", "allowedUntil": iso(allowed_until.replace(microsecond=0))})

    seat = db.session.get(Seat, seat_id)
    if seat is None:
        raise ApiError("NOT_FOUND")
    # R3 座位生命周期
    if seat.state != SEAT_AVAILABLE:
        raise ApiError("SEAT_UNAVAILABLE", fields={"seatState": seat.state})
    # 房间停用
    if not seat.room.active:
        raise ApiError("ROOM_INACTIVE", fields={"roomId": seat.room_id})
    # R-open 开放时间（endsAt 落次日 00:00 表示到 24:00 关闭，R-2）
    end_hour = effective_end_hour(ends_at)
    if starts_at.hour < seat.room.open_hour or end_hour > seat.room.close_hour:
        raise ApiError("RESERVATION_BLOCKED", fields={
            "rule": "R-open", "ruleId": "BR-02",
            "openHour": seat.room.open_hour, "closeHour": seat.room.close_hour})
    return seat, params


def create_locked(user, seat, starts_at, ends_at, params: dict) -> Reservation:
    """锁内复核 + 写入（权威判定；调用方须已处于 immediate_transaction 且完成幂等登记）。

    锁序 L4→L1→L2（§5.4）：幂等已由装饰层登记；此处先复核本人配额/重叠（L1），
    再复核目标格占用（L2），最后写入。顺序不可交换（AV-09 逐列构造可判定）。
    """
    # R6/R5 本人配额与重叠（L1）
    active_count = Reservation.query.filter(
        Reservation.user_id == user.id, Reservation.status.in_(ACTIVE_STATUSES)).count()
    if active_count >= int(params["maxActiveReservations"]):
        raise ApiError("RESERVATION_BLOCKED", fields={
            "rule": "R6", "ruleId": "P-1", "activeReservationCount": active_count})
    overlap = Reservation.query.filter(
        Reservation.user_id == user.id, Reservation.status.in_(ACTIVE_STATUSES),
        Reservation.starts_at < ends_at, Reservation.ends_at > starts_at).all()
    if overlap:
        raise ApiError("RESERVATION_BLOCKED", fields={
            "rule": "R5", "ruleId": "F-2",
            "conflictReservationIds": [r.id for r in overlap]})

    # R4 目标格占用（L2）：改查 seat_occupancy（Q-02 模型），U-1 兜底
    slots = slot_range(starts_at, ends_at)
    taken = (SeatOccupancy.query
             .filter(SeatOccupancy.seat_id == seat.id,
                     SeatOccupancy.slot_start.in_(slots)).first())
    if taken is not None:
        raise ApiError("SLOT_TAKEN", fields={
            "seatId": seat.id, "startsAt": iso(starts_at), "endsAt": iso(ends_at),
            "reason": "OCCUPIED"})

    reservation = Reservation(user_id=user.id, seat_id=seat.id,
                              starts_at=starts_at, ends_at=ends_at, status=RSV_BOOKED)
    db.session.add(reservation)
    db.session.flush()  # 拿 reservation.id，保证占用行外键可用

    for slot in slots:
        db.session.add(SeatOccupancy(seat_id=seat.id, reservation_id=reservation.id,
                                     slot_start=slot))

    add_outbox("RESERVATION_CREATED", user.id, {
        "reservationId": reservation.id, "seatLabel": seat.label,
        "startsAt": iso(starts_at), "endsAt": iso(ends_at)})
    add_audit("RESERVATION_CREATED", target_type="reservation", target_id=reservation.id)
    return reservation


def active_count_of(user_id: int) -> int:
    return Reservation.query.filter(
        Reservation.user_id == user_id, Reservation.status.in_(ACTIVE_STATUSES)).count()


def cancel_locked(reservation: Reservation, reason: str | None) -> dict:
    """API-07：条件更新 → 删全部占用 → outbox/审计。前置（本人 BOOKED 且未过截止）由调用方校验。"""
    t_now = now()
    updated = (db.session.query(Reservation)
               .filter(Reservation.id == reservation.id, Reservation.status == RSV_BOOKED)
               .update({"status": "CANCELLED", "cancelled_at": t_now,
                        "cancel_reason": reason}, synchronize_session=False))
    if updated == 0:
        db.session.rollback()
        db.session.refresh(reservation)
        raise ApiError("RESERVATION_STATE_CONFLICT",
                       fields={"currentStatus": reservation.status})
    deleted = db.session.query(SeatOccupancy).filter(
        SeatOccupancy.reservation_id == reservation.id).delete(synchronize_session=False)
    add_outbox("RESERVATION_CANCELLED", reservation.user_id,
               {"reservationId": reservation.id})
    add_audit("RESERVATION_CANCELLED", target_type="reservation", target_id=reservation.id,
              reason=reason)
    db.session.flush()
    db.session.refresh(reservation)
    return {"deletedOccupancies": deleted}


def check_in_locked(reservation: Reservation) -> dict:
    """API-08：窗口双端含（Q-04 裁决 A）；checkedInAt 一次写入不可覆盖。"""
    _, params = rule_params.current()
    t_now = now()
    window_end = reservation.starts_at + timedelta(minutes=int(params["checkInWindowMinutes"]))
    if reservation.status == RSV_BOOKED:
        if not (reservation.starts_at <= t_now <= window_end):
            raise ApiError("CHECKIN_WINDOW_CLOSED", fields={
                "windowStart": iso(reservation.starts_at), "windowEnd": iso(window_end),
                "willBeViolated": t_now > window_end})
        updated = (db.session.query(Reservation)
                   .filter(Reservation.id == reservation.id,
                           Reservation.status == RSV_BOOKED,
                           Reservation.checked_in_at.is_(None))
                   .update({"status": RSV_CHECKED_IN, "checked_in_at": t_now},
                           synchronize_session=False))
        if updated == 0:
            # 并发重复签到：已是 CHECKED_IN，幂等返回，不覆盖 checkedInAt
            db.session.rollback()
            db.session.refresh(reservation)
            if reservation.status != RSV_CHECKED_IN:
                raise ApiError("RESERVATION_STATE_CONFLICT",
                               fields={"currentStatus": reservation.status})
        add_audit("RESERVATION_CHECKED_IN", target_type="reservation",
                  target_id=reservation.id)
    elif reservation.status != RSV_CHECKED_IN:
        raise ApiError("RESERVATION_STATE_CONFLICT",
                       fields={"currentStatus": reservation.status})
    db.session.refresh(reservation)
    return {"checkedInAt": reservation.checked_in_at, "endsAt": reservation.ends_at}


def check_out_locked(reservation: Reservation) -> dict:
    """API-09：now<=endsAt；早退 now < ends-阈值（严格小于，D-8）；
    释放后续整点 slot_start > now（Q-05 裁决 B），seatFreedFrom = 被删最小 slot_start。"""
    from . import violation_service
    _, params = rule_params.current()
    t_now = now()
    if t_now > reservation.ends_at:
        raise ApiError("CHECKOUT_NOT_ALLOWED", fields={"endsAt": iso(reservation.ends_at)})

    updated = (db.session.query(Reservation)
               .filter(Reservation.id == reservation.id,
                       Reservation.status == RSV_CHECKED_IN)
               .update({"status": "COMPLETED", "checked_out_at": t_now},
                       synchronize_session=False))
    if updated == 0:
        db.session.rollback()
        db.session.refresh(reservation)
        raise ApiError("RESERVATION_STATE_CONFLICT",
                       fields={"currentStatus": reservation.status})

    early = t_now < reservation.ends_at - timedelta(
        minutes=int(params["earlyLeaveThresholdMinutes"]))
    violation_outcome = None
    if early:
        # ST-04：早退只记违约+计分+黑名单；预约保持 COMPLETED，占用走"后续整点"释放（Q-05）
        violation_outcome = violation_service.record_violation(
            reservation, "EARLY_LEAVE", on_duplicate="skip",
            set_status=False, release_all=False,
            audit_action="VIOLATION_EARLY_LEAVE")

    freed_rows = (db.session.query(SeatOccupancy)
                  .filter(SeatOccupancy.reservation_id == reservation.id,
                          SeatOccupancy.slot_start > t_now)
                  .order_by(SeatOccupancy.slot_start).all())
    seat_freed_from = freed_rows[0].slot_start if freed_rows else None
    for row in freed_rows:
        db.session.delete(row)

    add_outbox("RESERVATION_COMPLETED", reservation.user_id,
               {"reservationId": reservation.id, "early": early})
    add_audit("RESERVATION_CHECKED_OUT", target_type="reservation",
              target_id=reservation.id)
    db.session.flush()
    db.session.refresh(reservation)
    return {
        "early": early,
        "violationCreated": violation_outcome is not None,
        "violationId": violation_outcome.violation.id if violation_outcome else None,
        "seatFreedFrom": seat_freed_from,
    }


def eligibility(reservation: Reservation) -> dict:
    """服务端统一计算三 eligibility（§4.10 固定算式，与 API-07/08/09 共用同一参数源）。"""
    _, params = rule_params.current()
    t_now = now()
    cancel_eligible = (reservation.status == RSV_BOOKED and t_now <= reservation.starts_at -
                        timedelta(minutes=int(params["cancelLeadMinutes"])))
    checkin_eligible = (reservation.status == RSV_BOOKED and reservation.starts_at <= t_now <=
                        reservation.starts_at + timedelta(minutes=int(params["checkInWindowMinutes"])))
    checkout_eligible = (reservation.status == RSV_CHECKED_IN and t_now <= reservation.ends_at)
    return {"cancelEligible": cancel_eligible,
            "checkInEligible": checkin_eligible,
            "checkOutEligible": checkout_eligible}
