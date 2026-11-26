"""TASK-S2-05 修复 ADR-2 停用联动缺陷（TC-API13d-02）。

任务卡目标：管理员停用座位时，该座位"已签到"的预约被终止、用户收到明确提示、违约表
0 新增，不再出现"座位已停用但预约仍显示使用中"的状态矛盾。

说明：经核实当前实现已把 CHECKED_IN 终止为 COMPLETED + terminated_by + 删占用 + outbox，
且不记违约（violationsCreated 恒 0），即 ADR-2 缺陷已修复。本文件验证该已修复行为。
"""

from __future__ import annotations

from studyroom.extensions import db as _db
from studyroom.models import (ACTIVE_STATUSES, AuditLog, NotificationOutbox,
                              Reservation, Violation)

from tests.conftest import book, day, ik


def _make_checked_in(app, client, headers, seat_id):
    """预约并直接置 CHECKED_IN（避开签到窗口）。"""
    r = book(client, headers, seat_id, f"{day(1)}T10:00:00", f"{day(1)}T11:00:00")
    rid = r.get_json()["data"]["reservationId"]
    with client.application.app_context():
        _db.session.get(Reservation, rid).status = "CHECKED_IN"
        _db.session.commit()
    return rid


def _disable_seat(client, adm_headers, seat_id, reason="停用"):
    r = client.patch(f"/api/v1/admin/seats/{seat_id}",
                     headers={**adm_headers, "Idempotency-Key": ik()},
                     json={"state": "DISABLED", "reason": reason})
    assert r.status_code == 200, r.get_json()
    return r.get_json()["data"]


def test_disable_seat_terminates_checked_in(app, client, stu_headers, adm_headers,
                                            room_and_seat):
    """TC-API13d-02：已签到预约被终止为 COMPLETED + terminated_by，占用删除。"""
    from studyroom.models import SeatOccupancy
    _, seat_id = room_and_seat
    rid = _make_checked_in(app, client, stu_headers, seat_id)
    d = _disable_seat(client, adm_headers, seat_id)
    assert d["endedCheckIns"] == 1 and d["cancelledReservations"] == 0
    assert d["violationsCreated"] == 0
    with client.application.app_context():
        res = _db.session.get(Reservation, rid)
        assert res.status == "COMPLETED" and res.terminated_by == "SEAT_DISABLED"
        assert SeatOccupancy.query.filter_by(reservation_id=rid).count() == 0


def test_disable_seat_violation_rows_unchanged(app, client, stu_headers, adm_headers,
                                               room_and_seat):
    """ADR-2 硬断言：停用前后 Violation 表行数不变（0 新增）。"""
    _, seat_id = room_and_seat
    _make_checked_in(app, client, stu_headers, seat_id)
    with client.application.app_context():
        before = Violation.query.count()
    _disable_seat(client, adm_headers, seat_id)
    with client.application.app_context():
        assert Violation.query.count() == before


def test_disable_seat_no_active_reservation_left(app, client, stu_headers, adm_headers,
                                                 room_and_seat):
    """停用后该座位不存在 已签到/待签到 预约（消除状态矛盾）。"""
    _, seat_id = room_and_seat
    _make_checked_in(app, client, stu_headers, seat_id)
    _disable_seat(client, adm_headers, seat_id)
    with client.application.app_context():
        n = Reservation.query.filter(
            Reservation.seat_id == seat_id,
            Reservation.status.in_(ACTIVE_STATUSES)).count()
        assert n == 0


def test_disable_seat_repeat_idempotent(app, client, stu_headers, adm_headers,
                                        room_and_seat):
    """重复停用（state 已 DISABLED）不重复终止、不重复写审计。"""
    _, seat_id = room_and_seat
    rid = _make_checked_in(app, client, stu_headers, seat_id)
    first = _disable_seat(client, adm_headers, seat_id)
    assert first["endedCheckIns"] == 1
    second = _disable_seat(client, adm_headers, seat_id)
    assert second["endedCheckIns"] == 0  # state 未变，不再联动
    with client.application.app_context():
        assert AuditLog.query.filter_by(action="SEAT_DISABLED").count() == 1
        res = _db.session.get(Reservation, rid)
        assert res.status == "COMPLETED" and res.terminated_by == "SEAT_DISABLED"


def test_disable_room_terminates_checked_in(app, client, stu_headers, adm_headers,
                                            room_and_seat):
    """停用自习室同样终止已签到预约、不记违约。"""
    from studyroom.models import SeatOccupancy
    room_id, seat_id = room_and_seat
    rid = _make_checked_in(app, client, stu_headers, seat_id)
    r = client.patch(f"/api/v1/admin/rooms/{room_id}",
                     headers={**adm_headers, "Idempotency-Key": ik()},
                     json={"active": False, "reason": "翻新"})
    assert r.status_code == 200
    d = r.get_json()["data"]
    assert d["endedCheckIns"] == 1 and d["affectedReservations"] == 0
    with client.application.app_context():
        res = _db.session.get(Reservation, rid)
        assert res.status == "COMPLETED" and res.terminated_by == "ROOM_DISABLED"
        assert SeatOccupancy.query.filter_by(reservation_id=rid).count() == 0


def test_disable_checked_in_writes_notice_outbox(app, client, stu_headers, adm_headers,
                                                 room_and_seat):
    """终止时写 SEAT_UNAVAILABLE outbox（用户提示落点）。"""
    _, seat_id = room_and_seat
    rid = _make_checked_in(app, client, stu_headers, seat_id)
    _disable_seat(client, adm_headers, seat_id)
    with client.application.app_context():
        box = NotificationOutbox.query.filter_by(event_type="SEAT_UNAVAILABLE").all()
        assert len(box) == 1
        assert f'"reservationId": {rid}' in box[0].payload_json
