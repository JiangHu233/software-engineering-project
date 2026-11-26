"""TASK-S1-02 预约创建（API-06）、取消（API-07）。

失败语义表逐条（AV-09：每条规则可区分且零数据变化）+ 幂等 + 取消边界。
并发用例（TC-API06-09）依赖 TASK-S1-03 harness，本任务 skip，不在本文件新增并发断言。
"""
import uuid
from datetime import datetime, timedelta

from tests.conftest import book, day, ik


def _count_all(client):
    from studyroom.models import Reservation, SeatOccupancy
    from studyroom.extensions import db as _db
    with client.application.app_context():
        return Reservation.query.count(), SeatOccupancy.query.count()


def test_create_ok_and_split(client, stu_headers, room_and_seat):
    _, seat_id = room_and_seat
    r = book(client, stu_headers, seat_id, f"{day(1)}T10:00:00", f"{day(1)}T12:00:00")
    d = r.get_json()["data"]
    assert r.status_code == 201
    assert d["status"] == "BOOKED"
    assert d["checkInWindow"]["end"].endswith("10:15:00")
    assert d["activeReservationCount"] == 1
    assert r.headers.get("Location", "").startswith("/api/v1/reservations/")
    # 跨小时拆分：2 条占用
    from studyroom.models import SeatOccupancy
    from studyroom.extensions import db as _db
    with client.application.app_context():
        assert SeatOccupancy.query.filter_by(seat_id=seat_id).count() == 2


def test_r2_past(client, stu_headers, room_and_seat):
    _, seat_id = room_and_seat
    past = (datetime.now() - timedelta(hours=3)).replace(minute=0, second=0, microsecond=0)
    r = book(client, stu_headers, seat_id, past.strftime("%Y-%m-%dT%H:%M:%S"),
             (past + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S"), expect=422)
    err = r.get_json()["error"]
    assert err["code"] == "RESERVATION_BLOCKED" and err["fields"]["rule"] == "R2"


def test_r8_beyond_window(client, stu_headers, room_and_seat):
    _, seat_id = room_and_seat
    r = book(client, stu_headers, seat_id, f"{day(30)}T10:00:00", f"{day(30)}T11:00:00", expect=422)
    err = r.get_json()["error"]
    assert err["fields"]["rule"] == "R8"
    assert "allowedUntil" in err["fields"]


def test_seat_unavailable(client, stu_headers, adm_headers, room_and_seat):
    _, seat_id = room_and_seat
    r = client.patch(f"/api/v1/admin/seats/{seat_id}",
                     headers={**adm_headers, "Idempotency-Key": ik()},
                     json={"state": "MAINTENANCE", "reason": "维修"})
    assert r.status_code == 200
    r = book(client, stu_headers, seat_id, f"{day(1)}T10:00:00", f"{day(1)}T11:00:00", expect=422)
    assert r.get_json()["error"]["code"] == "SEAT_UNAVAILABLE"
    assert r.get_json()["error"]["fields"]["seatState"] == "MAINTENANCE"


def test_room_inactive(client, stu_headers, adm_headers, room_and_seat):
    room_id, seat_id = room_and_seat
    r = client.patch(f"/api/v1/admin/rooms/{room_id}",
                     headers={**adm_headers, "Idempotency-Key": ik()},
                     json={"active": False, "reason": "停用"})
    assert r.status_code == 200
    r = book(client, stu_headers, seat_id, f"{day(1)}T10:00:00", f"{day(1)}T11:00:00", expect=422)
    assert r.get_json()["error"]["code"] == "ROOM_INACTIVE"


def test_r_open_outside_hours(client, stu_headers, room_and_seat):
    _, seat_id = room_and_seat
    r = book(client, stu_headers, seat_id, f"{day(1)}T07:00:00", f"{day(1)}T08:00:00", expect=422)
    err = r.get_json()["error"]
    assert err["fields"]["rule"] == "R-open"
    # closeHour=22：约到 22:00-23:00 应被拒
    r = book(client, stu_headers, seat_id, f"{day(1)}T22:00:00", f"{day(1)}T23:00:00", expect=422)
    assert r.get_json()["error"]["fields"]["rule"] == "R-open"


def test_close_hour_24_next_day_midnight(client, stu_headers, room_and_seat):
    """R-2：closeHour=24 时 endsAt 写次日 T00:00:00，跨日不算跨开放边界。"""
    from studyroom.models import Room
    from studyroom.extensions import db as _db
    room_id, seat_id = room_and_seat
    with client.application.app_context():
        _db.session.get(Room, room_id).close_hour = 24
        _db.session.commit()
    # Q-18 白名单：fixture 外层会话 identity map 仍持有 close_hour=22 的 Room，
    # 若不 expire，后续请求复用该会话会读到陈旧值导致假失败（非实现缺陷）。
    _db.session.expire_all()
    next_day = day(2)
    d0 = (datetime.strptime(next_day, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    r = book(client, stu_headers, seat_id, f"{d0}T22:00:00", f"{next_day}T00:00:00")
    assert r.status_code == 201


def test_non_hour_rejected(client, stu_headers, room_and_seat):
    _, seat_id = room_and_seat
    r = book(client, stu_headers, seat_id, f"{day(1)}T10:30:00", f"{day(1)}T11:30:00", expect=400)
    assert r.get_json()["error"]["code"] == "VALIDATION_FAILED"


def test_timezone_suffix_rejected(client, stu_headers, room_and_seat):
    _, seat_id = room_and_seat
    r = book(client, stu_headers, seat_id, f"{day(1)}T10:00:00+08:00", f"{day(1)}T11:00:00", expect=400)
    assert r.get_json()["error"]["code"] == "VALIDATION_FAILED"


def test_slot_taken_by_other(client, stu_headers, stu2_headers, room_and_seat):
    _, seat_id = room_and_seat
    book(client, stu_headers, seat_id, f"{day(1)}T10:00:00", f"{day(1)}T11:00:00")
    r = book(client, stu2_headers, seat_id, f"{day(1)}T10:00:00", f"{day(1)}T11:00:00", expect=409)
    err = r.get_json()["error"]
    assert err["code"] == "SLOT_TAKEN" and err["fields"]["reason"] == "OCCUPIED"


def test_r5_self_overlap_other_seat(client, stu_headers, room_and_seat):
    from studyroom.models import Seat
    from studyroom.extensions import db as _db
    room_id, seat_id = room_and_seat
    with client.application.app_context():
        s2 = Seat(room_id=room_id, label="T02", zone="A区", state="AVAILABLE")
        _db.session.add(s2)
        _db.session.commit()
        sid2 = s2.id
    book(client, stu_headers, seat_id, f"{day(1)}T10:00:00", f"{day(1)}T12:00:00")
    r = book(client, stu_headers, sid2, f"{day(1)}T11:00:00", f"{day(1)}T13:00:00", expect=422)
    err = r.get_json()["error"]
    assert err["fields"]["rule"] == "R5"
    assert "conflictReservationIds" in err["fields"]


def test_r6_quota_exceeded(client, stu_headers, room_and_seat):
    from studyroom.models import Seat
    from studyroom.extensions import db as _db
    room_id, seat_id = room_and_seat
    with client.application.app_context():
        for i in range(3, 6):
            _db.session.add(Seat(room_id=room_id, label=f"T{i:02d}", zone="A区"))
        _db.session.commit()
        seats = [s.id for s in Seat.query.filter_by(room_id=room_id).all()]
    book(client, stu_headers, seats[0], f"{day(1)}T08:00:00", f"{day(1)}T09:00:00")
    book(client, stu_headers, seats[1], f"{day(1)}T10:00:00", f"{day(1)}T11:00:00")
    r = book(client, stu_headers, seats[2], f"{day(1)}T13:00:00", f"{day(1)}T14:00:00", expect=422)
    err = r.get_json()["error"]
    assert err["fields"]["rule"] == "R6"
    assert err["fields"]["activeReservationCount"] == 2


def test_blacklisted_blocked(client, stu_headers, room_and_seat):
    from studyroom.models import Blacklist
    from studyroom.extensions import db as _db
    from datetime import datetime as dt
    _, seat_id = room_and_seat
    with client.application.app_context():
        from studyroom.models import User
        uid = User.query.filter_by(username="stu1").first().id
        _db.session.add(Blacklist(user_id=uid, source="AUTO",
                                  blacklist_until=dt.now() + timedelta(days=3)))
        _db.session.commit()
    r = book(client, stu_headers, seat_id, f"{day(5)}T10:00:00", f"{day(5)}T11:00:00", expect=422)
    err = r.get_json()["error"]
    assert err["code"] == "BLACKLISTED"
    assert "blacklistUntil" in err["fields"]


def test_zero_data_change_on_failure(client, stu_headers, room_and_seat):
    _, seat_id = room_and_seat
    before = _count_all(client)
    book(client, stu_headers, seat_id, f"{day(1)}T07:00:00", f"{day(1)}T08:00:00", expect=422)
    assert _count_all(client) == before


def test_idempotency_replay_and_conflict(client, stu_headers, room_and_seat):
    _, seat_id = room_and_seat
    key = ik()
    body = {"seatId": seat_id, "startsAt": f"{day(1)}T10:00:00", "endsAt": f"{day(1)}T11:00:00"}
    r1 = client.post("/api/v1/reservations", headers={**stu_headers, "Idempotency-Key": key}, json=body)
    assert r1.status_code == 201
    r2 = client.post("/api/v1/reservations", headers={**stu_headers, "Idempotency-Key": key}, json=body)
    assert r2.status_code == 201
    assert r2.get_json()["meta"]["replayed"] is True
    assert r2.get_json()["data"]["reservationId"] == r1.get_json()["data"]["reservationId"]
    # 同键异体
    r3 = client.post("/api/v1/reservations", headers={**stu_headers, "Idempotency-Key": key},
                     json={**body, "endsAt": f"{day(1)}T12:00:00"})
    assert r3.status_code == 422
    assert r3.get_json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"
    from studyroom.models import Reservation
    from studyroom.extensions import db as _db
    with client.application.app_context():
        assert Reservation.query.count() == 1


def test_missing_idempotency_key(client, stu_headers, room_and_seat):
    _, seat_id = room_and_seat
    r = client.post("/api/v1/reservations", headers=stu_headers,
                    json={"seatId": seat_id, "startsAt": f"{day(1)}T10:00:00",
                          "endsAt": f"{day(1)}T11:00:00"})
    assert r.status_code == 400
    assert "Idempotency-Key" in r.get_json()["error"]["fields"]


def test_cancel_boundary_exactly_30min(client, app, stu_headers, room_and_seat):
    """Q-03 裁决 C：now <= startsAt - 30min（含等于）可取消。"""
    _, seat_id = room_and_seat
    r = book(client, stu_headers, seat_id, f"{day(1)}T10:00:00", f"{day(1)}T11:00:00")
    rid = r.get_json()["data"]["reservationId"]
    # 把 starts_at 调到 now+31min：starts-30min 落在 now+1min，留出执行余量，
    # 避免 now+30min 因毫秒级耗时落到 30min 内造成时序假失败（阈值两侧各留余量）。
    from tests.conftest import shift_reservation
    shift_reservation(app, rid, starts_at=datetime.now().replace(microsecond=0) + timedelta(minutes=31),
                      ends_at=datetime.now().replace(microsecond=0) + timedelta(minutes=91),
                      sync_occupancy=True)
    r = client.post(f"/api/v1/reservations/{rid}/cancel",
                    headers={**stu_headers, "Idempotency-Key": ik()}, json={"reason": "有课"})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["data"]["status"] == "CANCELLED"
    # 29 分钟 → CANCEL_TOO_LATE
    r2 = book(client, stu_headers, seat_id, f"{day(2)}T10:00:00", f"{day(2)}T11:00:00")
    rid2 = r2.get_json()["data"]["reservationId"]
    shift_reservation(app, rid2,
                      starts_at=datetime.now().replace(microsecond=0) + timedelta(minutes=29),
                      ends_at=datetime.now().replace(microsecond=0) + timedelta(minutes=89),
                      sync_occupancy=True)
    r = client.post(f"/api/v1/reservations/{rid2}/cancel",
                    headers={**stu_headers, "Idempotency-Key": ik()}, json={})
    assert r.status_code == 422
    assert r.get_json()["error"]["code"] == "CANCEL_TOO_LATE"
    assert "cancelDeadline" in r.get_json()["error"]["fields"]


def test_cancel_frees_slot_for_others(client, stu_headers, stu2_headers, room_and_seat):
    _, seat_id = room_and_seat
    r = book(client, stu_headers, seat_id, f"{day(1)}T10:00:00", f"{day(1)}T11:00:00")
    rid = r.get_json()["data"]["reservationId"]
    r = client.post(f"/api/v1/reservations/{rid}/cancel",
                    headers={**stu_headers, "Idempotency-Key": ik()}, json={})
    assert r.status_code == 200
    r = book(client, stu2_headers, seat_id, f"{day(1)}T10:00:00", f"{day(1)}T11:00:00")
    assert r.status_code == 201


def test_cancel_other_user_forbidden(client, stu_headers, stu2_headers, room_and_seat):
    _, seat_id = room_and_seat
    r = book(client, stu_headers, seat_id, f"{day(1)}T10:00:00", f"{day(1)}T11:00:00")
    rid = r.get_json()["data"]["reservationId"]
    r = client.post(f"/api/v1/reservations/{rid}/cancel",
                    headers={**stu2_headers, "Idempotency-Key": ik()}, json={})
    assert r.status_code == 403
    assert r.get_json()["error"]["code"] == "FORBIDDEN_OBJECT"


def test_full_student_chain(client, stu_headers, stu2_headers, room_and_seat):
    """TASK-S1-02 首页目标：看座位 → 约 → 取消 → 他人能约到（一条链路贯通）。"""
    room_id, seat_id = room_and_seat
    slot = (f"{day(1)}T10:00:00", f"{day(1)}T11:00:00")
    # 1) 看座位：学生能在房间座位列表里看到该座位
    r = client.get(f"/api/v1/rooms/{room_id}/seats?date={day(1)}", headers=stu_headers)
    assert r.status_code == 200
    assert any(i["seatId"] == seat_id for i in r.get_json()["data"]["items"])
    # 2) 约：学生一预约成功
    r = book(client, stu_headers, seat_id, *slot)
    rid = r.get_json()["data"]["reservationId"]
    assert r.get_json()["data"]["status"] == "BOOKED"
    # 此时他人约不到（SLOT_TAKEN）
    r = book(client, stu2_headers, seat_id, *slot, expect=409)
    assert r.get_json()["error"]["code"] == "SLOT_TAKEN"
    # 3) 取消：学生一取消预约
    r = client.post(f"/api/v1/reservations/{rid}/cancel",
                    headers={**stu_headers, "Idempotency-Key": ik()}, json={"reason": "有课"})
    assert r.status_code == 200
    assert r.get_json()["data"]["status"] == "CANCELLED"
    # 4) 他人能约到：同一时段释放后可被学生二预约成功
    r = book(client, stu2_headers, seat_id, *slot)
    assert r.status_code == 201
    assert r.get_json()["data"]["status"] == "BOOKED"
