from datetime import datetime, timedelta

from app import Reservation, Room, Seat, User, Violation, db
from conftest import login


def next_hour(offset=2):
    # Keep examples inside opening hours regardless of when the suite runs.
    return (datetime.now() + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)


def post_booking(client, seat_id, start, end):
    return client.post("/reservations", data={
        "seat_id": str(seat_id), "starts_at": start.isoformat(timespec="minutes"),
        "ends_at": end.isoformat(timespec="minutes"),
    }, follow_redirects=True)


def test_login_roles_and_auth_required(client):
    assert client.get("/").status_code == 302
    assert "座位预约" in login(client).get_data(as_text=True)
    assert client.get("/admin").status_code == 403
    admin_client = client.application.test_client()
    login(admin_client, "admin", "admin123")
    assert admin_client.get("/admin").status_code == 200


def test_bad_login_does_not_authenticate(client):
    response = client.post("/login", data={"username": "student", "password": "wrong"}, follow_redirects=True)
    assert "账号或密码不正确" in response.get_data(as_text=True)
    assert response.request.path == "/login"


def test_booking_overlap_double_booking_and_ownership(client, app):
    login(client)
    with app.app_context():
        seat = Seat.query.first()
        seat_id = seat.id
        start = next_hour()
        other = User(username="other", display_name="另一位学生", role="student")
        other.set_password("other123")
        db.session.add(other)
        db.session.commit()
    post_booking(client, seat_id, start, start + timedelta(hours=1))
    with app.app_context():
        assert Reservation.query.filter_by(user_id=User.query.filter_by(username="student").first().id, status="booked").count() == 1
    post_booking(client, seat_id, start + timedelta(minutes=30), start + timedelta(hours=2))
    with app.app_context():
        assert Reservation.query.filter_by(user_id=User.query.filter_by(username="student").first().id, status="booked").count() == 1

    intruder = app.test_client()
    login(intruder, "other", "other123")
    with app.app_context():
        booking = Reservation.query.first()
        booking_id = booking.id
    assert intruder.post(f"/reservations/{booking_id}/cancel").status_code == 403
    assert intruder.post(f"/reservations/{booking_id}/check-in").status_code == 403


def test_booking_window_hours_and_active_limit(client, app):
    login(client)
    with app.app_context():
        seats = Seat.query.order_by(Seat.id).limit(3).all()
        ids = [s.id for s in seats]
        start = next_hour(2)
        uid = User.query.filter_by(username="student").first().id
        db.session.add_all([
            Reservation(user_id=uid, seat_id=ids[1], starts_at=start + timedelta(hours=2), ends_at=start + timedelta(hours=3)),
            Reservation(user_id=uid, seat_id=ids[2], starts_at=start + timedelta(hours=4), ends_at=start + timedelta(hours=5)),
        ])
        db.session.commit()
    response = post_booking(client, ids[0], start, start + timedelta(hours=1))
    assert "最多持有2个有效预约" in response.get_data(as_text=True)
    with app.app_context():
        db.session.query(Reservation).delete()
        db.session.commit()
    past_start = (datetime.now() - timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    response = post_booking(client, ids[0], past_start, past_start + timedelta(hours=1))
    assert "未来整点" in response.get_data(as_text=True)
    too_far = (datetime.now() + timedelta(days=8)).replace(hour=10, minute=0, second=0, microsecond=0)
    post_booking(client, ids[0], too_far, too_far + timedelta(hours=1))
    with app.app_context():
        assert Reservation.query.filter(Reservation.status.in_(["booked", "checked_in"])).count() == 0


def test_cancel_at_30_minute_boundary(client, app):
    login(client)
    with app.app_context():
        user = User.query.filter_by(username="student").first()
        seat = Seat.query.first()
        booking = Reservation(user_id=user.id, seat_id=seat.id,
                              starts_at=datetime.now() + timedelta(minutes=29),
                              ends_at=datetime.now() + timedelta(hours=2))
        db.session.add(booking)
        db.session.commit()
        booking_id = booking.id
    assert "至少30分钟" in client.post(f"/reservations/{booking_id}/cancel", follow_redirects=True).get_data(as_text=True)
    with app.app_context():
        booking = db.session.get(Reservation, booking_id)
        booking.starts_at = datetime.now() + timedelta(minutes=31)
        db.session.commit()
    assert "预约已取消" in client.post(f"/reservations/{booking_id}/cancel", follow_redirects=True).get_data(as_text=True)


def test_checkin_checkout_and_early_checkout_violation(client, app):
    login(client)
    with app.app_context():
        user = User.query.filter_by(username="student").first()
        seat = Seat.query.first()
        now = datetime.now()
        booking = Reservation(user_id=user.id, seat_id=seat.id,
                              starts_at=now - timedelta(minutes=5), ends_at=now + timedelta(hours=1))
        db.session.add(booking)
        db.session.commit()
        booking_id = booking.id
    assert "签到成功" in client.post(f"/reservations/{booking_id}/check-in", follow_redirects=True).get_data(as_text=True)
    with app.app_context():
        booking = db.session.get(Reservation, booking_id)
        booking.ends_at = datetime.now() + timedelta(minutes=45)
        db.session.commit()
    assert "记为违约" in client.post(f"/reservations/{booking_id}/check-out", follow_redirects=True).get_data(as_text=True)
    with app.app_context():
        assert Violation.query.filter_by(reservation_id=booking_id).count() == 1
        assert db.session.get(Reservation, booking_id).status == "violation"


def test_auto_violation_is_idempotent_and_blacklist_applies(client, app):
    login(client)
    with app.app_context():
        user = User.query.filter_by(username="student").first()
        seat = Seat.query.first()
        now = datetime.now()
        bookings = [Reservation(user_id=user.id, seat_id=seat.id,
                                starts_at=now - timedelta(minutes=30 + i),
                                ends_at=now + timedelta(hours=1 + i)) for i in range(3)]
        db.session.add_all(bookings)
        db.session.commit()
        booking_ids = [b.id for b in bookings]
    client.get("/")
    client.get("/")
    with app.app_context():
        user = User.query.filter_by(username="student").first()
        assert Violation.query.count() == 3
        assert user.is_blacklisted
        assert user.blacklist_entries == 1
        assert {r.status for r in Reservation.query.filter(Reservation.id.in_(booking_ids)).all()} == {"violation"}
    start = next_hour(4)
    with app.app_context():
        available = Seat.query.filter(Seat.state == "available").order_by(Seat.id).all()
        free_id = next(s.id for s in available if not Reservation.query.filter_by(seat_id=s.id).first())
    response = post_booking(client, free_id, start, start + timedelta(hours=1))
    assert "预约限制期" in response.get_data(as_text=True)


def test_admin_room_seat_and_manual_violation_audit(client, app):
    login(client, "admin", "admin123")
    assert "自习室管理台" in client.get("/admin").get_data(as_text=True)
    assert "自习室已创建" in client.post("/admin/rooms", data={"name": "静心室", "location": "实验楼 2F"}, follow_redirects=True).get_data(as_text=True)
    with app.app_context():
        student = User.query.filter_by(username="student").first()
        seat = Seat.query.first()
        booking = Reservation(user_id=student.id, seat_id=seat.id,
                              starts_at=datetime.now() - timedelta(minutes=20),
                              ends_at=datetime.now() + timedelta(hours=1), status="checked_in")
        db.session.add(booking)
        db.session.commit()
        booking_id = booking.id
    response = client.post(f"/admin/reservations/{booking_id}/violation", data={"reason": "现场占用他人座位"}, follow_redirects=True)
    assert "违约已登记" in response.get_data(as_text=True)
    with app.app_context():
        violation = Violation.query.filter_by(reservation_id=booking_id).one()
        assert violation.reason == "现场占用他人座位"
        assert violation.operator.username == "admin"


def test_admin_cannot_register_without_reason_or_active_booking(client, app):
    login(client, "admin", "admin123")
    with app.app_context():
        user = User.query.filter_by(username="student").first()
        seat = Seat.query.first()
        booking = Reservation(user_id=user.id, seat_id=seat.id, starts_at=next_hour(), ends_at=next_hour() + timedelta(hours=1))
        db.session.add(booking)
        db.session.commit()
        booking_id = booking.id
    response = client.post(f"/admin/reservations/{booking_id}/violation", data={"reason": ""}, follow_redirects=True)
    assert "正在使用中的预约" in response.get_data(as_text=True)
    with app.app_context():
        assert Violation.query.count() == 0
