"""TASK-S2-04 定时任务与启动补偿（T-1/T-2/T-3）。

目标：停机再启动后，期间应判的违约全部补齐且不重复；黑名单期满以时间为准自动恢复，
不依赖状态字段被刷新。

扫描函数（scan_no_show / scan_no_checkout / expire_blacklists）在 app.app_context() 内
直接调用（它们内部各自开 immediate_transaction、写 ScanRun）。判定纯按时间比较。
"""

from __future__ import annotations

from datetime import timedelta

from studyroom.extensions import db as _db
from studyroom.models import Blacklist, ScanRun, User, Violation

from tests.conftest import book, day, ik, shift_reservation


def _make_no_show(app, client, headers, seat_id):
    """构造一条超窗未签到（BOOKED，starts_at 已过 30 分钟）的预约。"""
    r = book(client, headers, seat_id, f"{day(1)}T10:00:00", f"{day(1)}T11:00:00")
    rid = r.get_json()["data"]["reservationId"]
    from datetime import datetime
    shift_reservation(app, rid, starts_at=datetime.now() - timedelta(minutes=30),
                      ends_at=datetime.now() + timedelta(minutes=60),
                      status="BOOKED", sync_occupancy=True)
    return rid


def _make_no_checkout(app, client, headers, seat_id):
    """构造一条超宽限未签退（CHECKED_IN，ends_at 已过 30 分钟）的预约。"""
    r = book(client, headers, seat_id, f"{day(1)}T10:00:00", f"{day(1)}T11:00:00")
    rid = r.get_json()["data"]["reservationId"]
    from datetime import datetime
    shift_reservation(app, rid, starts_at=datetime.now() - timedelta(minutes=120),
                      ends_at=datetime.now() - timedelta(minutes=30),
                      status="CHECKED_IN", sync_occupancy=True)
    return rid


def _add_blacklist(client, days_from_now):
    from studyroom.services.timeutil import now as now_fn
    with client.application.app_context():
        uid = User.query.filter_by(username="stu1").first().id
        _db.session.add(Blacklist(user_id=uid, source="AUTO",
                                  blacklist_until=now_fn() + timedelta(days=days_from_now)))
        _db.session.commit()
        return uid


def test_no_show_scan_marks_violated_once(app, client, stu_headers, room_and_seat):
    """TC-E2E-01：超窗未签到→补跑判违约，恰 1 条 NO_SHOW。"""
    from studyroom.tasks.scan_timeouts import scan_no_show
    from studyroom.models import Reservation
    _, seat_id = room_and_seat
    rid = _make_no_show(app, client, stu_headers, seat_id)
    with app.app_context():
        result = scan_no_show()
        assert result["succeeded"] == 1
        assert _db.session.get(Reservation, rid).status == "VIOLATED"
        assert Violation.query.filter_by(reservation_id=rid).count() == 1
        assert Violation.query.filter_by(reservation_id=rid).first().kind == "NO_SHOW"


def test_no_checkout_scan_marks_violated(app, client, stu_headers, room_and_seat):
    """超宽限未签退→scan_no_checkout→VIOLATED + NO_CHECKOUT。"""
    from studyroom.tasks.scan_timeouts import scan_no_checkout
    from studyroom.models import Reservation
    _, seat_id = room_and_seat
    rid = _make_no_checkout(app, client, stu_headers, seat_id)
    with app.app_context():
        result = scan_no_checkout()
        assert result["succeeded"] == 1
        assert _db.session.get(Reservation, rid).status == "VIOLATED"
        assert Violation.query.filter_by(reservation_id=rid).first().kind == "NO_CHECKOUT"


def test_scan_twice_no_duplicate(app, client, stu_headers, room_and_seat):
    """TC-E2E-01：补跑两次不重复（等价'连续两次启动仅补一次'）。"""
    from studyroom.tasks.scan_timeouts import scan_no_show
    _, seat_id = room_and_seat
    _make_no_show(app, client, stu_headers, seat_id)
    with app.app_context():
        assert scan_no_show()["succeeded"] == 1
        assert Violation.query.count() == 1
        assert scan_no_show()["succeeded"] == 0  # 已 VIOLATED，不再拾取
        assert Violation.query.count() == 1


def test_scan_writes_scan_run_record(app, client, stu_headers, room_and_seat):
    """每次扫描写 ScanRun 记录，scanned/succeeded/failed 正确。"""
    from studyroom.tasks.scan_timeouts import scan_no_show
    _, seat_id = room_and_seat
    _make_no_show(app, client, stu_headers, seat_id)
    with app.app_context():
        scan_no_show()
        run = ScanRun.query.order_by(ScanRun.id.desc()).first()
        assert run is not None and run.status == "DONE"
        assert run.task_name == "T-1_no_show"
        assert run.scanned >= 1 and run.succeeded >= 1 and run.failed == 0


def test_expired_blacklist_released_on_expire(app, client, stu_headers, room_and_seat):
    """blacklistUntil 已过→expire_blacklists 写 released_at。"""
    from studyroom.tasks.blacklist_expire import expire_blacklists
    uid = _add_blacklist(client, days_from_now=-1)
    with app.app_context():
        result = expire_blacklists()
        assert result["released"] == 1
        bl = Blacklist.query.filter_by(user_id=uid).first()
        assert bl.released_at is not None


def test_expired_blacklist_allows_booking(app, client, stu_headers, room_and_seat):
    """TC-E2E-02①：黑名单期满释放后，预约放行（不产生新违约）。"""
    from studyroom.tasks.blacklist_expire import expire_blacklists
    _, seat_id = room_and_seat
    _add_blacklist(client, days_from_now=-1)
    with app.app_context():
        expire_blacklists()
    r = book(client, stu_headers, seat_id, f"{day(2)}T10:00:00", f"{day(2)}T11:00:00")
    assert r.status_code == 201, r.get_json()


def test_active_blacklist_not_released(app, client, stu_headers, room_and_seat):
    """blacklistUntil 未过→不释放、仍被拦（BLACKLISTED）。"""
    from studyroom.tasks.blacklist_expire import expire_blacklists
    _, seat_id = room_and_seat
    _add_blacklist(client, days_from_now=3)
    with app.app_context():
        result = expire_blacklists()
        assert result["released"] == 0
    r = book(client, stu_headers, seat_id, f"{day(5)}T10:00:00", f"{day(5)}T11:00:00",
             expect=422)
    assert r.get_json()["error"]["code"] == "BLACKLISTED"


def test_blacklist_expire_uses_time_not_status(app, client, stu_headers, room_and_seat):
    """期满判定只看时间：读路径 blacklist_until>now，不依赖 released_at 被刷新。"""
    from studyroom.services import credit_service
    from studyroom.services.timeutil import now as now_fn
    uid = _add_blacklist(client, days_from_now=-1)
    with app.app_context():
        # 未调用 expire，released_at 仍为 None，但读路径按时间判定已过期 → 不受限
        assert Blacklist.query.filter_by(user_id=uid).first().released_at is None
        assert credit_service.active_blacklist(uid) is None
