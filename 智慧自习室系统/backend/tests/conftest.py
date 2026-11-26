from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from studyroom.app import create_app  # noqa: E402
from studyroom.extensions import db as _db  # noqa: E402
from studyroom import cli as _cli  # noqa: E402
from studyroom.models import Reservation, Seat, SeatOccupancy, User  # noqa: E402


@pytest.fixture()
def app(tmp_path):
    app = create_app("test", {
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'test.db'}",
    })
    with app.app_context():
        _db.create_all()
        _cli._seed_base()
        yield app
        _db.session.remove()


@pytest.fixture()
def client(app):
    return app.test_client()


def _login(client, username, password):
    r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.get_json()
    token = r.get_json()["data"]["token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def stu_headers(client):
    # seed-demo 会补齐演示学生，但为保证用例独立，这里单独创建
    with client.application.app_context():
        if not User.query.filter_by(username="stu1").first():
            u = User(username="stu1", display_name="学生一", role="STU")
            u.set_password("password1")
            _db.session.add(u)
            _db.session.commit()
    return _login(client, "stu1", "password1")


@pytest.fixture()
def stu2_headers(client):
    with client.application.app_context():
        if not User.query.filter_by(username="stu2").first():
            u = User(username="stu2", display_name="学生二", role="STU")
            u.set_password("password1")
            _db.session.add(u)
            _db.session.commit()
    return _login(client, "stu2", "password1")


@pytest.fixture()
def adm_headers(client):
    return _login(client, "admin", "admin123")


@pytest.fixture()
def room_and_seat(app):
    from studyroom.models import Room
    with app.app_context():
        room = Room(name="测试自习室", location="1F", open_hour=8, close_hour=22)
        _db.session.add(room)
        _db.session.commit()
        seat = Seat(room_id=room.id, label="T01", zone="A区", state="AVAILABLE")
        seat.features = ["电源"]
        _db.session.add(seat)
        _db.session.commit()
        yield room.id, seat.id


def ik() -> str:
    return str(uuid.uuid4())


def day(offset=1) -> str:
    return (datetime.now() + timedelta(days=offset)).strftime("%Y-%m-%d")


def book(client, headers, seat_id, start, end, expect=201):
    r = client.post("/api/v1/reservations", headers={**headers, "Idempotency-Key": ik()},
                    json={"seatId": seat_id, "startsAt": start, "endsAt": end})
    assert r.status_code == expect, r.get_json()
    return r


def shift_reservation(app, reservation_id, starts_at=None, ends_at=None, status=None,
                      sync_occupancy=False):
    """Q-18 白名单：只改库内时间字段构造场景（禁改服务器时间）。"""
    with app.app_context():
        res = _db.session.get(Reservation, reservation_id)
        if starts_at is not None:
            res.starts_at = starts_at
        if ends_at is not None:
            res.ends_at = ends_at
        if status is not None:
            res.status = status
        if sync_occupancy:
            _db.session.query(SeatOccupancy).filter(
                SeatOccupancy.reservation_id == reservation_id).delete()
            cur = res.starts_at
            while cur < res.ends_at:
                _db.session.add(SeatOccupancy(seat_id=res.seat_id,
                                              reservation_id=reservation_id, slot_start=cur))
                cur += timedelta(hours=1)
        _db.session.commit()
