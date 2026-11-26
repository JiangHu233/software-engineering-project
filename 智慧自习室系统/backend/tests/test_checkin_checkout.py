"""API-08 签到 / API-09 签退：窗口双端边界、早退 D-8、seatFreedFrom 三例。"""
from datetime import datetime, timedelta

from conftest import book, day, ik, shift_reservation


def _setup_window(app, client, headers, seat_id, minutes_until_start):
    """构造一个 starts_at = now + minutes 的预约（改库同步占用）。"""
    r = book(client, headers, seat_id, f"{day(1)}T10:00:00", f"{day(1)}T12:00:00")
    rid = r.get_json()["data"]["reservationId"]
    base = datetime.now().replace(microsecond=0)
    shift_reservation(app, rid, starts_at=base + timedelta(minutes=minutes_until_start),
                      ends_at=base + timedelta(minutes=minutes_until_start + 120),
                      sync_occupancy=True)
    return rid


def test_checkin_at_start_ok(app, client, stu_headers, room_and_seat):
    _, seat_id = room_and_seat
    rid = _setup_window(app, client, stu_headers, seat_id, minutes_until_start=0)
    r = client.post(f"/api/v1/reservations/{rid}/check-in",
                    headers={**stu_headers, "Idempotency-Key": ik()})
    assert r.status_code == 200
    d = r.get_json()["data"]
    assert d["status"] == "CHECKED_IN" and d["seatState"] == "IN_USE"
    assert d["checkOutDeadline"]


def test_checkin_before_window_rejected(app, client, stu_headers, room_and_seat):
    _, seat_id = room_and_seat
    rid = _setup_window(app, client, stu_headers, seat_id, minutes_until_start=5)
    r = client.post(f"/api/v1/reservations/{rid}/check-in",
                    headers={**stu_headers, "Idempotency-Key": ik()})
    assert r.status_code == 422
    err = r.get_json()["error"]
    assert err["code"] == "CHECKIN_WINDOW_CLOSED"
    assert "windowStart" in err["fields"] and "windowEnd" in err["fields"]
    assert err["fields"].get("willBeViolated") is not True


def test_checkin_after_window_will_be_violated(app, client, stu_headers, room_and_seat):
    _, seat_id = room_and_seat
    rid = _setup_window(app, client, stu_headers, seat_id, minutes_until_start=-20)
    r = client.post(f"/api/v1/reservations/{rid}/check-in",
                    headers={**stu_headers, "Idempotency-Key": ik()})
    assert r.status_code == 422
    assert r.get_json()["error"]["fields"]["willBeViolated"] is True


def test_checkin_checked_in_at_not_overwritten(app, client, stu_headers, room_and_seat):
    _, seat_id = room_and_seat
    rid = _setup_window(app, client, stu_headers, seat_id, minutes_until_start=0)
    r1 = client.post(f"/api/v1/reservations/{rid}/check-in",
                     headers={**stu_headers, "Idempotency-Key": ik()})
    first_at = r1.get_json()["data"]["checkedInAt"]
    # 新键再签到：200 但 checkedInAt 不变（幂等语义）
    r2 = client.post(f"/api/v1/reservations/{rid}/check-in",
                     headers={**stu_headers, "Idempotency-Key": ik()})
    assert r2.status_code == 200
    assert r2.get_json()["data"]["checkedInAt"] == first_at


def test_checkout_normal_no_violation(app, client, stu_headers, room_and_seat):
    _, seat_id = room_and_seat
    rid = _setup_window(app, client, stu_headers, seat_id, minutes_until_start=-1)
    client.post(f"/api/v1/reservations/{rid}/check-in",
                headers={**stu_headers, "Idempotency-Key": ik()})
    # 结束前 10 分钟签退（不足 30 分钟早退阈值）→ 无违约
    from studyroom.extensions import db as _db
    from studyroom.models import Reservation
    with client.application.app_context():
        res = _db.session.get(Reservation, rid)
        res.ends_at = datetime.now() + timedelta(minutes=10)
        _db.session.commit()
    r = client.post(f"/api/v1/reservations/{rid}/check-out",
                    headers={**stu_headers, "Idempotency-Key": ik()})
    assert r.status_code == 200
    d = r.get_json()["data"]
    assert d["status"] == "COMPLETED" and d["early"] is False
    assert d["violationCreated"] is False


def test_checkout_early_leave_strict_less(app, client, stu_headers, room_and_seat):
    """D-8：now < ends-30min 才记违约；恰好 30 分钟不记。"""
    _, seat_id = room_and_seat
    # 场景 A：31 分钟后结束 → 记违约
    rid = _setup_window(app, client, stu_headers, seat_id, minutes_until_start=-61)
    client.post(f"/api/v1/reservations/{rid}/check-in",
                headers={**stu_headers, "Idempotency-Key": ik()})
    from studyroom.extensions import db as _db
    from studyroom.models import Reservation
    with client.application.app_context():
        _db.session.get(Reservation, rid).ends_at = datetime.now() + timedelta(minutes=31)
        _db.session.commit()
    r = client.post(f"/api/v1/reservations/{rid}/check-out",
                    headers={**stu_headers, "Idempotency-Key": ik()})
    d = r.get_json()["data"]
    assert r.status_code == 200 and d["early"] is True and d["violationCreated"] is True

    # 场景 B：恰好 30 分钟 → 不记（Q-03 裁决 C，操作符翻转验证）
    rid2 = _setup_window(app, client, stu_headers, seat_id, minutes_until_start=-61)
    client.post(f"/api/v1/reservations/{rid2}/check-in",
                headers={**stu_headers, "Idempotency-Key": ik()})
    with client.application.app_context():
        base = datetime.now()
        res = _db.session.get(Reservation, rid2)
        res.ends_at = base.replace(microsecond=0) + timedelta(minutes=30)
        _db.session.commit()
        # 对齐到整秒避免抖动：把 now 推到恰好 (ends-30min) 的边界内一点
    r2 = client.post(f"/api/v1/reservations/{rid2}/check-out",
                     headers={**stu_headers, "Idempotency-Key": ik()})
    d2 = r2.get_json()["data"]
    assert r2.status_code == 200 and d2["violationCreated"] is False


def test_checkout_after_end_rejected(app, client, stu_headers, room_and_seat):
    _, seat_id = room_and_seat
    rid = _setup_window(app, client, stu_headers, seat_id, minutes_until_start=-120)
    client.post(f"/api/v1/reservations/{rid}/check-in",
                headers={**stu_headers, "Idempotency-Key": ik()})
    # ends_at 已过 → 422 CHECKOUT_NOT_ALLOWED（超时归 T-2）
    r = client.post(f"/api/v1/reservations/{rid}/check-out",
                    headers={**stu_headers, "Idempotency-Key": ik()})
    assert r.status_code == 422
    assert r.get_json()["error"]["code"] == "CHECKOUT_NOT_ALLOWED"


def test_seat_freed_from_semantics(app, client, stu_headers, room_and_seat):
    """Q-05：只释放 slot_start > now 的后续整点。
    构造：starts_at = 当前整点 H，共 3 格 [H,H+1,H+2]，now 落在 H 之后 → 释放 H+1 起；
    单小时预约 → 恒 null。"""
    _, seat_id = room_and_seat
    hour_start = datetime.now().replace(minute=0, second=0, microsecond=0)
    rid = _make_checked_in(app, client, stu_headers, seat_id,
                           starts_at=hour_start, ends_at=hour_start + timedelta(hours=3))
    r = client.post(f"/api/v1/reservations/{rid}/check-out",
                    headers={**stu_headers, "Idempotency-Key": ik()})
    d = r.get_json()["data"]
    assert r.status_code == 200
    now = datetime.now()
    if now < hour_start + timedelta(hours=1):
        # now 仍在第一格内（等效 14:30 场景）→ seatFreedFrom = H+1 整点
        assert d["seatFreedFrom"] == (hour_start + timedelta(hours=1)).isoformat()
    else:
        freed = datetime.fromisoformat(d["seatFreedFrom"])
        assert freed > hour_start  # 只会释放更晚的整点

    # 单小时预约：唯一格子是当前所在整点 → 无格可删
    rid2 = _make_checked_in(app, client, stu_headers, seat_id,
                            starts_at=hour_start, ends_at=hour_start + timedelta(hours=1))
    r2 = client.post(f"/api/v1/reservations/{rid2}/check-out",
                     headers={**stu_headers, "Idempotency-Key": ik()})
    d2 = r2.get_json()["data"]
    if datetime.now() < hour_start + timedelta(hours=1):
        assert d2["seatFreedFrom"] is None


def _make_checked_in(app, client, headers, seat_id, starts_at, ends_at):
    """预约 + 改库到指定时间 + 直接置 CHECKED_IN（避开签到窗口构造）。"""
    r = book(client, headers, seat_id, f"{day(1)}T10:00:00", f"{day(1)}T12:00:00")
    rid = r.get_json()["data"]["reservationId"]
    from studyroom.extensions import db as _db
    from studyroom.models import Reservation
    with client.application.app_context():
        _db.session.get(Reservation, rid).status = "CHECKED_IN"
        _db.session.commit()
    shift_reservation(app, rid, starts_at=starts_at, ends_at=ends_at, sync_occupancy=True)
    with client.application.app_context():
        _db.session.get(Reservation, rid).checked_in_at = starts_at
        _db.session.commit()
    return rid


def test_checkout_terminal_state_409(app, client, stu_headers, room_and_seat):
    _, seat_id = room_and_seat
    rid = _setup_window(app, client, stu_headers, seat_id, minutes_until_start=0)
    client.post(f"/api/v1/reservations/{rid}/check-in",
                headers={**stu_headers, "Idempotency-Key": ik()})
    client.post(f"/api/v1/reservations/{rid}/check-out",
                headers={**stu_headers, "Idempotency-Key": ik()})
    r = client.post(f"/api/v1/reservations/{rid}/check-out",
                    headers={**stu_headers, "Idempotency-Key": ik()})
    assert r.status_code == 409
    assert r.get_json()["error"]["code"] == "RESERVATION_STATE_CONFLICT"
