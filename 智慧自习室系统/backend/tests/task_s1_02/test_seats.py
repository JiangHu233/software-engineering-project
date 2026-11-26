"""API-04/05：字段白名单、C-7 聚合优先级、日期越界、ROOM_CLOSED。"""
from datetime import timedelta

from tests.conftest import book, day, ik


def test_seat_response_field_whitelist(client, stu_headers, room_and_seat):
    """PS-01/NFR-08：学生端座位响应不得出现占用者身份字段。"""
    room_id, seat_id = room_and_seat
    book(client, stu_headers, seat_id, f"{day(1)}T10:00:00", f"{day(1)}T11:00:00")
    r = client.get(f"/api/v1/rooms/{room_id}/seats?date={day(1)}", headers=stu_headers)
    assert r.status_code == 200
    body = str(r.get_json())
    for forbidden in ("displayName", "username", "userId"):
        assert forbidden not in body
    item = r.get_json()["data"]["items"][0]
    assert set(item.keys()) == {"seatId", "seatLabel", "zone", "features", "state",
                                "roomId", "roomName", "openHour", "closeHour"}
    assert r.get_json()["meta"]["asOf"]


def test_state_aggregation_priority(client, stu_headers, room_and_seat):
    """R-3：窗口内多整点取最严重 DISABLED > MAINTENANCE > IN_USE > BOOKED > AVAILABLE。"""
    room_id, seat_id = room_and_seat
    book(client, stu_headers, seat_id, f"{day(1)}T10:00:00", f"{day(1)}T11:00:00")
    r = client.get(f"/api/v1/rooms/{room_id}/seats?date={day(1)}&from=10:00&to=14:00",
                   headers=stu_headers)
    states = {i["seatId"]: i["state"] for i in r.get_json()["data"]["items"]}
    assert states[seat_id] == "BOOKED"  # 10-11 被约 → 最严重 BOOKED

    r = client.get(f"/api/v1/rooms/{room_id}/seats?date={day(1)}&from=12:00&to=14:00",
                   headers=stu_headers)
    states = {i["seatId"]: i["state"] for i in r.get_json()["data"]["items"]}
    assert states[seat_id] == "AVAILABLE"


def test_date_out_of_range(client, stu_headers, room_and_seat):
    room_id, _ = room_and_seat
    r = client.get(f"/api/v1/rooms/{room_id}/seats?date=2030-01-01", headers=stu_headers)
    assert r.status_code == 400
    past = ("2020-01-01")
    r = client.get(f"/api/v1/rooms/{room_id}/seats?date={past}", headers=stu_headers)
    assert r.status_code == 400


def test_from_to_pair_and_on_hour(client, stu_headers, room_and_seat):
    room_id, _ = room_and_seat
    r = client.get(f"/api/v1/rooms/{room_id}/seats?date={day(1)}&from=10:00", headers=stu_headers)
    assert r.status_code == 400
    r = client.get(f"/api/v1/rooms/{room_id}/seats?date={day(1)}&from=10:30&to=12:00",
                   headers=stu_headers)
    assert r.status_code == 400  # 非整点
    r = client.get(f"/api/v1/rooms/{room_id}/seats?date={day(1)}&from=10:00&to=24:00",
                   headers=stu_headers)
    assert r.status_code == 400  # 24:00 禁写（§3.1）


def test_zone_filter_validated(client, stu_headers, room_and_seat):
    room_id, _ = room_and_seat
    r = client.get(f"/api/v1/rooms/{room_id}/seats?date={day(1)}&zone=不存在", headers=stu_headers)
    assert r.status_code == 400


def test_room_404_and_room_closed(client, stu_headers, room_and_seat):
    room_id, seat_id = room_and_seat
    r = client.get(f"/api/v1/rooms/9999/seats?date={day(1)}", headers=stu_headers)
    assert r.status_code == 404
    # 房间停用 → API-05 全格 ROOM_CLOSED（200 不报 422）
    from studyroom.extensions import db as _db
    from studyroom.models import Room
    with client.application.app_context():
        _db.session.get(Room, room_id).active = False
        _db.session.commit()
    # 与外层 fixture 会话同根因：expire 掉 identity map，避免请求读到陈旧的 room.active=True
    _db.session.expire_all()
    r = client.get(f"/api/v1/seats/{seat_id}/availability?date={day(1)}", headers=stu_headers)
    assert r.status_code == 200
    assert all(s["status"] == "ROOM_CLOSED" for s in r.get_json()["data"]["slots"])


def test_availability_reserved_by_me(client, stu_headers, stu2_headers, room_and_seat):
    _, seat_id = room_and_seat
    book(client, stu_headers, seat_id, f"{day(1)}T10:00:00", f"{day(1)}T12:00:00")
    r = client.get(f"/api/v1/seats/{seat_id}/availability?date={day(1)}", headers=stu_headers)
    slots = {s["hour"]: s for s in r.get_json()["data"]["slots"]}
    d = day(1)
    from datetime import datetime
    hour = datetime.strptime(d, "%Y-%m-%d").hour  # 0 → 但我们约的是 10 点，直接看 10
    assert slots[10]["status"] == "BOOKED" and slots[10]["reservedByMe"] is True
    assert slots[11]["reservedByMe"] is True
    assert slots[13]["status"] == "AVAILABLE" and slots[13]["reservedByMe"] is False
    # 他人视角：reservedByMe 恒 false，且无占用者信息
    r2 = client.get(f"/api/v1/seats/{seat_id}/availability?date={day(1)}", headers=stu2_headers)
    slots2 = {s["hour"]: s for s in r2.get_json()["data"]["slots"]}
    assert slots2[10]["status"] == "BOOKED" and slots2[10]["reservedByMe"] is False


def test_pagination_limit(client, stu_headers, room_and_seat):
    room_id, _ = room_and_seat
    r = client.get(f"/api/v1/rooms/{room_id}/seats?date={day(1)}&pageSize=101", headers=stu_headers)
    assert r.status_code == 400
    r = client.get(f"/api/v1/rooms/{room_id}/seats?date={day(1)}&pageSize=100", headers=stu_headers)
    assert r.status_code == 200
