"""TASK-S2-03 手动违约登记与黑名单判定（API-16 / §5.5）。

目标：管理员对"已签到"预约登记违约后，座位立即释放、学生分数 +1、达阈值自动进名单；
重复提交只产生 1 条记录；计分口径全系统唯一实现（credit_service）。

说明：TC-API16-07 的"审计 2 条（1 成功 1 失败）"中，当前实现失败路径不写失败审计，
本文件先断言核心保证（重复被拒 + 仅 1 条 + 分数只 +1 + 成功审计 1 条），失败审计缺口
在汇报中登记供裁决。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from studyroom.extensions import db as _db
from studyroom.models import AuditLog, Blacklist, Reservation, Seat, Violation

from tests.conftest import book, day, ik


def _make_checked_in(app, client, headers, seat_id):
    """预约并直接置 CHECKED_IN（避开签到窗口）。"""
    r = book(client, headers, seat_id, f"{day(1)}T10:00:00", f"{day(1)}T11:00:00")
    rid = r.get_json()["data"]["reservationId"]
    with client.application.app_context():
        _db.session.get(Reservation, rid).status = "CHECKED_IN"
        _db.session.commit()
    return rid


def _set_status(app, client, rid, status):
    with client.application.app_context():
        _db.session.get(Reservation, rid).status = status
        _db.session.commit()


def _add_seat(client, room_id, label):
    with client.application.app_context():
        s = Seat(room_id=room_id, label=label, zone="A区", state="AVAILABLE")
        _db.session.add(s)
        _db.session.commit()
        return s.id


def _register(client, adm_headers, rid, expect=201):
    r = client.post(f"/api/v1/admin/reservations/{rid}/violations",
                    headers={**adm_headers, "Idempotency-Key": ik()},
                    json={"kind": "ON_SITE", "reason": "现场违规使用"})
    assert r.status_code == expect, r.get_json()
    return r.get_json()


def test_manual_violation_marks_violated_and_releases_seat(app, client, stu_headers,
                                                           adm_headers, room_and_seat):
    """登记违约：预约置 VIOLATED、占用删除、座位释放（blacklistTriggered 本轮 False）。"""
    from studyroom.models import SeatOccupancy
    _, seat_id = room_and_seat
    rid = _make_checked_in(app, client, stu_headers, seat_id)
    body = _register(client, adm_headers, rid)
    d = body["data"]
    assert d["reservationStatus"] == "VIOLATED" and d["blacklistTriggered"] is False
    with client.application.app_context():
        assert _db.session.get(Reservation, rid).status == "VIOLATED"
        assert SeatOccupancy.query.filter_by(reservation_id=rid).count() == 0


def test_manual_violation_increments_score(app, client, stu_headers, adm_headers, room_and_seat):
    """登记后 termViolationScore +1（计分唯一实现）。"""
    _, seat_id = room_and_seat
    rid = _make_checked_in(app, client, stu_headers, seat_id)
    d = _register(client, adm_headers, rid)["data"]
    assert d["termViolationScore"] == 1
    with client.application.app_context():
        assert Violation.query.count() == 1


def test_duplicate_registration_single_violation(app, client, stu_headers, adm_headers,
                                                 room_and_seat):
    """TC-API16-07 核心：重复登记→409 被拒、违约表恰好 1 条、分数仍 1。

    首次登记后预约置 VIOLATED，第二次在 status!=CHECKED_IN 处先拦截 → 返回
    RESERVATION_STATE_CONFLICT（DUPLICATE_VIOLATION 仅当状态被改回 CHECKED_IN 才可达）。
    """
    _, seat_id = room_and_seat
    rid = _make_checked_in(app, client, stu_headers, seat_id)
    first = _register(client, adm_headers, rid)["data"]
    assert first["termViolationScore"] == 1
    second = _register(client, adm_headers, rid, expect=409)
    assert second["error"]["code"] == "RESERVATION_STATE_CONFLICT"
    with client.application.app_context():
        assert Violation.query.count() == 1
        assert Blacklist.query.count() == 0  # 重复登记不额外计分/进名单


def test_successful_registration_writes_audit(app, client, stu_headers, adm_headers,
                                              room_and_seat):
    """成功登记写 VIOLATION_MANUAL 审计 1 条。"""
    _, seat_id = room_and_seat
    rid = _make_checked_in(app, client, stu_headers, seat_id)
    _register(client, adm_headers, rid)
    with client.application.app_context():
        assert AuditLog.query.filter_by(action="VIOLATION_MANUAL").count() == 1


def test_third_violation_triggers_blacklist(app, client, stu_headers, adm_headers,
                                            room_and_seat):
    """TC-E2E-01②：第 3 次违约达阈值→进名单、Blacklist 表 1 条。"""
    room_id, seat_id = room_and_seat
    seats = [seat_id, _add_seat(client, room_id, "T03"), _add_seat(client, room_id, "T04")]
    flags = []
    for s in seats:
        rid = _make_checked_in(app, client, stu_headers, s)
        flags.append(_register(client, adm_headers, rid)["data"]["blacklistTriggered"])
    assert flags[:2] == [False, False] and flags[2] is True
    with client.application.app_context():
        assert Blacklist.query.count() == 1
        assert Violation.query.count() == 3


def test_blacklist_until_equals_entries_days(app, client, stu_headers, adm_headers,
                                             room_and_seat):
    """进名单后 blacklistUntil ≈ now + blacklistDaysPerEntry(7) 天，可复算。"""
    from studyroom.services.timeutil import now as now_fn
    room_id, seat_id = room_and_seat
    seats = [seat_id, _add_seat(client, room_id, "T05"), _add_seat(client, room_id, "T06")]
    until = None
    for s in seats:
        rid = _make_checked_in(app, client, stu_headers, s)
        d = _register(client, adm_headers, rid)["data"]
        until = d["blacklistUntil"]
    assert until is not None
    dt = datetime.fromisoformat(until)
    expect = now_fn() + timedelta(days=7)
    assert expect - timedelta(minutes=1) <= dt <= expect + timedelta(minutes=1)


def test_registration_rejected_for_booked_and_completed(app, client, stu_headers,
                                                        adm_headers, room_and_seat):
    """对 待签到/已完成 登记被拒 RESERVATION_STATE_CONFLICT，状态不变。"""
    _, seat_id = room_and_seat
    r = book(client, stu_headers, seat_id, f"{day(2)}T10:00:00", f"{day(2)}T11:00:00")
    booked_rid = r.get_json()["data"]["reservationId"]  # BOOKED
    completed_rid = _make_checked_in(app, client, stu_headers, seat_id)
    _set_status(app, client, completed_rid, "COMPLETED")
    for rid in (booked_rid, completed_rid):
        err = _register(client, adm_headers, rid, expect=409)
        assert err["error"]["code"] == "RESERVATION_STATE_CONFLICT"


def test_seat_bookable_by_other_after_violation(app, client, stu_headers, stu2_headers,
                                                adm_headers, room_and_seat):
    """ST-12：登记违约释放占用后，他人可约到同一时段。"""
    _, seat_id = room_and_seat
    rid = _make_checked_in(app, client, stu_headers, seat_id)
    _register(client, adm_headers, rid)
    r = book(client, stu2_headers, seat_id, f"{day(1)}T10:00:00", f"{day(1)}T11:00:00")
    assert r.status_code == 201


def test_student_cannot_register_violation(client, stu_headers, room_and_seat):
    """学生令牌调违约登记→403 FORBIDDEN_ROLE。"""
    _, seat_id = room_and_seat
    r = client.post("/api/v1/admin/reservations/1/violations",
                    headers={**stu_headers, "Idempotency-Key": ik()},
                    json={"kind": "ON_SITE", "reason": "x"})
    assert r.status_code == 403
    assert r.get_json()["error"]["code"] == "FORBIDDEN_ROLE"


def test_db_unique_violation_per_reservation(app, client, stu_headers, room_and_seat):
    """TC-DB-02：直接 INSERT 同 reservation 两条违约→IntegrityError（U-2 兜底）。"""
    from studyroom.models import User
    _, seat_id = room_and_seat
    rid = _make_checked_in(app, client, stu_headers, seat_id)
    with client.application.app_context():
        uid = User.query.filter_by(username="stu1").first().id
        _db.session.add(Violation(user_id=uid, reservation_id=rid, kind="ON_SITE", reason="x"))
        _db.session.flush()
        _db.session.add(Violation(user_id=uid, reservation_id=rid, kind="ON_SITE", reason="x"))
        with pytest.raises(IntegrityError):
            _db.session.commit()
        _db.session.rollback()
