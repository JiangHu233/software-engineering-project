"""TASK-S1-01 数据模型与唯一约束（数据库层兜底）测试。

目标（任务卡）：即使绕过应用层直接写库，数据库也要拒绝：
    U-1 同座位同时段双占
    U-2 同预约双违约
    U-3 同房重号座位、U-4 同名自习室
    U-6 幂等键重复、U-7 参数版本重复
    U-8 同用户多行"未关闭"黑名单（部分唯一索引）

本任务只证明约束真正生效，不改模型。"测试失败先判责"：约束测试失败先判断
是约束没建还是测试写法有误，不得放宽模型约束或修改需求。

按课程要求，本任务用例数控制在 10 条以内。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from studyroom.extensions import db as _db
from studyroom.models import (AuditLog, Blacklist, IdempotencyRecord, NotificationOutbox,
                              Reservation, Room, RuleParameter, Seat, SeatOccupancy,
                              User, Violation, add_outbox)

# ---------------------------------------------------------------- 辅助


@pytest.fixture()
def base_data(app):
    """一个房间、一个座位、一个学生、一条有效预约（供各约束测试复用）。"""
    with app.app_context():
        room = Room(name="约束测试室", location="1F", open_hour=8, close_hour=22)
        _db.session.add(room)
        _db.session.flush()
        seat = Seat(room_id=room.id, label="A01", zone="A区", state="AVAILABLE")
        _db.session.add(seat)
        _db.session.flush()
        stu = User.query.filter_by(username="stu1").first()
        if stu is None:
            stu = User(username="stu1", display_name="约束学生", role="STU")
            stu.set_password("password1")
            _db.session.add(stu)
            _db.session.flush()
        slot = (datetime.now() + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
        rsv = Reservation(user_id=stu.id, seat_id=seat.id,
                          starts_at=slot, ends_at=slot + timedelta(hours=1))
        _db.session.add(rsv)
        _db.session.flush()
        _db.session.commit()
        yield {"room": room, "seat": seat, "student": stu, "rsv": rsv,
               "slot": slot}


def insert_occupancy(seat_id: int, reservation_id: int, slot_start) -> None:
    _db.session.add(SeatOccupancy(seat_id=seat_id, reservation_id=reservation_id,
                                  slot_start=slot_start))
    _db.session.commit()


# ================================================================ U-1 / U-2（核心）


def test_u1_rejects_same_seat_same_slot(base_data):
    """TC-DB-01：同座位同时段插两条占用，第二条必须 IntegrityError。"""
    d = base_data
    _db.session.rollback()
    insert_occupancy(d["seat"].id, d["rsv"].id, d["slot"])
    with pytest.raises(IntegrityError):
        insert_occupancy(d["seat"].id, d["rsv"].id, d["slot"])
    _db.session.rollback()


def test_u1_allows_different_slot_and_different_seat(base_data, app):
    """约束只锁 (seat, slot) 二元组：不同 slot 或不同座位都允许。"""
    d = base_data
    _db.session.rollback()
    insert_occupancy(d["seat"].id, d["rsv"].id, d["slot"])
    insert_occupancy(d["seat"].id, d["rsv"].id, d["slot"] + timedelta(hours=1))
    with app.app_context():
        seat2 = Seat(room_id=d["room"].id, label="A02", zone="A区", state="AVAILABLE")
        _db.session.add(seat2)
        _db.session.flush()
        _db.session.commit()
        insert_occupancy(seat2.id, d["rsv"].id, d["slot"])


def test_u2_rejects_duplicate_violation_per_reservation(base_data):
    """TC-DB-02：同一预约插两条违约，第二条必须 IntegrityError。"""
    d = base_data
    _db.session.rollback()
    _db.session.add(Violation(user_id=d["student"].id, reservation_id=d["rsv"].id,
                              kind="NO_SHOW", reason="t"))
    _db.session.commit()
    with pytest.raises(IntegrityError):
        _db.session.add(Violation(user_id=d["student"].id, reservation_id=d["rsv"].id,
                                  kind="EARLY_LEAVE", reason="t2"))
        _db.session.commit()
    _db.session.rollback()


# ================================================================ U-3 / U-4 / U-6 / U-7


def test_u3_rejects_same_room_same_label(base_data):
    """U-3：同房重号座位必须拒绝。"""
    d = base_data
    _db.session.rollback()
    dup = Seat(room_id=d["room"].id, label=d["seat"].label, zone="A区", state="AVAILABLE")
    _db.session.add(dup)
    with pytest.raises(IntegrityError):
        _db.session.commit()
    _db.session.rollback()


def test_u4_rejects_duplicate_room_name(app):
    """U-4：自习室同名必须拒绝。"""
    with app.app_context():
        _db.session.add(Room(name="同名室", location="1F", open_hour=8, close_hour=22))
        _db.session.commit()
        _db.session.add(Room(name="同名室", location="2F", open_hour=8, close_hour=22))
        with pytest.raises(IntegrityError):
            _db.session.commit()
        _db.session.rollback()


def test_u6_rejects_same_idempotency_key(base_data):
    """U-6：同一 (actor, method, path, key) 幂等键重复必须拒绝。"""
    d = base_data
    _db.session.rollback()
    now = datetime.now()
    _db.session.add(IdempotencyRecord(actor_id=d["student"].id, method="POST",
                                      path="/api/v1/reservations", key="k1",
                                      request_hash="h", expires_at=now + timedelta(hours=1)))
    _db.session.commit()
    with pytest.raises(IntegrityError):
        _db.session.add(IdempotencyRecord(actor_id=d["student"].id, method="POST",
                                          path="/api/v1/reservations", key="k1",
                                          request_hash="h", expires_at=now + timedelta(hours=1)))
        _db.session.commit()
    _db.session.rollback()


def test_u7_rejects_same_rule_param_version(base_data):
    """U-7：同一 scope 同一 version 参数重复必须拒绝。"""
    _db.session.rollback()
    r1 = RuleParameter(scope="GLOBAL", version=999, created_by=None, reason="t")
    r1.params = {"maxActiveReservations": 2}
    _db.session.add(r1)
    _db.session.commit()
    with pytest.raises(IntegrityError):
        r2 = RuleParameter(scope="GLOBAL", version=999, created_by=None, reason="t2")
        r2.params = {"maxActiveReservations": 2}
        _db.session.add(r2)
        _db.session.commit()
    _db.session.rollback()


# ================================================================ U-8 / 审计 / outbox / 索引


def test_u8_rejects_two_active_blacklists_per_user(base_data):
    """U-8：同一用户两条未关闭黑名单必须拒绝（部分唯一索引只锁未关闭行）。"""
    d = base_data
    _db.session.rollback()
    _db.session.add(Blacklist(user_id=d["student"].id,
                              blacklist_until=datetime.now() + timedelta(days=7)))
    _db.session.commit()
    with pytest.raises(IntegrityError):
        _db.session.add(Blacklist(user_id=d["student"].id,
                                  blacklist_until=datetime.now() + timedelta(days=14)))
        _db.session.commit()
    _db.session.rollback()


def test_audit_append_and_outbox_rows(base_data):
    """审计只增不改 + 写接口事务内插 outbox 行（不投递）。"""
    d = base_data
    _db.session.rollback()
    a1 = AuditLog(actor_id=d["student"].id, actor_role="STU", action="RESERVATION_CREATE")
    _db.session.add(a1)
    _db.session.commit()
    a2 = AuditLog(actor_id=d["student"].id, actor_role="STU", action="RESERVATION_CREATE")
    _db.session.add(a2)
    _db.session.commit()
    assert a2.id > a1.id  # 只增
    add_outbox("RESERVATION_CREATED", d["student"].id, {"reservationId": d["rsv"].id})
    _db.session.commit()
    row = NotificationOutbox.query.filter_by(event_type="RESERVATION_CREATED").first()
    assert row is not None and row.delivered_at is None


def test_unique_indexes_present_in_sqlite(app):
    """关键唯一约束确实以 unique 索引落库（U-1/U-2 的建表级证明）。"""
    with app.app_context():
        for table in ("seat_occupancy", "violation"):
            idx = _db.session.execute(text(f"PRAGMA index_list('{table}')")).fetchall()
            assert any(r[2] == 1 for r in idx), f"{table} 缺唯一索引"
        tables = [r[0] for r in _db.session.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()]
        for expected in ("seat_occupancy", "violation", "blacklist", "idempotency_record",
                         "audit_log", "notification_outbox", "rule_parameter"):
            assert expected in tables, f"缺表 {expected}"
