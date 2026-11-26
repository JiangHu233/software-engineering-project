"""TASK-S1-04 资源维护与停用联动（API-13a/b/c/d）——核心子集。

目标：管理员停用座位/自习室后，其未来预约全部取消、该资源不再可约、每一步留痕；
停用不记违约（ADR-2，violationsCreated 恒 0）；座位编号/自习室名唯一性被数据库与
接口双重拦截。

说明：本文件为 S1-04 的核心子集（约一半）。已签到预约的终止验收归属 TASK-S2-05，
不在本任务裁决；剩余用例（唯一性接口层、学生越权、审计计数等）按需补齐。
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from tests.conftest import book, day, ik

DISABLED = {"state": "DISABLED"}
MAINT = {"state": "MAINTENANCE"}


def _disable_seat(client, adm_headers, seat_id, reason="维修"):
    r = client.patch(f"/api/v1/admin/seats/{seat_id}",
                     headers={**adm_headers, "Idempotency-Key": ik()},
                     json={**DISABLED, "reason": reason})
    assert r.status_code == 200, r.get_json()
    return r.get_json()["data"]


def test_disable_seat_cancels_future_booked(client, stu_headers, adm_headers, room_and_seat):
    """停用座位：未来 BOOKED 预约被取消、占用删除。"""
    from studyroom.models import Reservation, SeatOccupancy
    from studyroom.extensions import db as _db
    _, seat_id = room_and_seat
    r = book(client, stu_headers, seat_id, f"{day(1)}T10:00:00", f"{day(1)}T11:00:00")
    rid = r.get_json()["data"]["reservationId"]
    d = _disable_seat(client, adm_headers, seat_id)
    assert d["cancelledReservations"] == 1 and d["endedCheckIns"] == 0
    with client.application.app_context():
        assert _db.session.get(Reservation, rid).status == "CANCELLED"
        assert SeatOccupancy.query.filter_by(reservation_id=rid).count() == 0


def test_disable_seat_no_violation_created(client, stu_headers, adm_headers, room_and_seat):
    """ADR-2：停用不记违约——Violation 表 0 新增、响应 violationsCreated==0。"""
    from studyroom.models import Violation
    from studyroom.extensions import db as _db
    _, seat_id = room_and_seat
    book(client, stu_headers, seat_id, f"{day(1)}T10:00:00", f"{day(1)}T11:00:00")
    d = _disable_seat(client, adm_headers, seat_id)
    assert d["violationsCreated"] == 0
    with client.application.app_context():
        assert Violation.query.count() == 0


def test_disable_room_cancels_future_and_not_bookable(client, stu_headers, adm_headers,
                                                      room_and_seat):
    """停用自习室：未来预约取消、再约 ROOM_INACTIVE。"""
    from studyroom.models import Reservation
    from studyroom.extensions import db as _db
    room_id, seat_id = room_and_seat
    r = book(client, stu_headers, seat_id, f"{day(1)}T10:00:00", f"{day(1)}T11:00:00")
    rid = r.get_json()["data"]["reservationId"]
    resp = client.patch(f"/api/v1/admin/rooms/{room_id}",
                        headers={**adm_headers, "Idempotency-Key": ik()},
                        json={"active": False, "reason": "翻新"})
    assert resp.status_code == 200
    assert resp.get_json()["data"]["affectedReservations"] == 1
    with client.application.app_context():
        assert _db.session.get(Reservation, rid).status == "CANCELLED"
    r = book(client, stu_headers, seat_id, f"{day(2)}T10:00:00", f"{day(2)}T11:00:00",
             expect=422)
    assert r.get_json()["error"]["code"] == "ROOM_INACTIVE"


def test_seat_label_unique_at_db_layer(client, room_and_seat):
    """唯一性（DB 层）：绕过应用层直接 INSERT 同 (room_id, label) → IntegrityError（U-3）。"""
    from studyroom.models import Seat
    from studyroom.extensions import db as _db
    room_id, _ = room_and_seat
    with client.application.app_context():
        _db.session.add(Seat(room_id=room_id, label="T01", zone="A区", state="AVAILABLE"))
        with pytest.raises(IntegrityError):
            _db.session.commit()
        _db.session.rollback()


def test_maintenance_does_not_cancel_reservations(client, stu_headers, adm_headers,
                                                  room_and_seat):
    """Q-07：MAINTENANCE 不动任何预约（cancelledReservations==0，预约仍 BOOKED）。"""
    from studyroom.models import Reservation
    from studyroom.extensions import db as _db
    _, seat_id = room_and_seat
    r = book(client, stu_headers, seat_id, f"{day(1)}T10:00:00", f"{day(1)}T11:00:00")
    rid = r.get_json()["data"]["reservationId"]
    resp = client.patch(f"/api/v1/admin/seats/{seat_id}",
                        headers={**adm_headers, "Idempotency-Key": ik()},
                        json={**MAINT, "reason": "检修"})
    assert resp.status_code == 200
    assert resp.get_json()["data"]["cancelledReservations"] == 0
    with client.application.app_context():
        assert _db.session.get(Reservation, rid).status == "BOOKED"


def test_disable_requires_reason(client, adm_headers, room_and_seat):
    """停用/维护必须填原因：缺 reason → REASON_REQUIRED。"""
    _, seat_id = room_and_seat
    for state in ("DISABLED", "MAINTENANCE"):
        r = client.patch(f"/api/v1/admin/seats/{seat_id}",
                         headers={**adm_headers, "Idempotency-Key": ik()},
                         json={"state": state})
        assert r.status_code == 400
        assert r.get_json()["error"]["code"] == "REASON_REQUIRED"
