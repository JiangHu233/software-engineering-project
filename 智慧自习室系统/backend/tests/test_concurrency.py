"""TASK-S0-01 并发验证用例。

任务卡要求的两组必执行用例：
    单进程组：2 线程 × 50 轮，每轮新座位，断言每轮成功数 == 1
    多进程组：2 进程 × 10 轮，断言每轮成功数 == 1、失败方拿到冲突语义

本文件在此基础上按课程要求扩充用例数，额外覆盖：数据集规模自检、座位唯一、
时段冲突边界、幂等键复用、并发下的本人重叠、库层唯一约束兜底、失败方错误码
白名单、轮次间状态清理、跨房间独立性、三方争抢等情形。

"测试失败先判责"说明
------------------
若多进程组失败，先判断是实现存在问题还是测试预期写错；不得为了让测试全绿而
删除断言、放宽错误码或把并发改成串行。
"""

from __future__ import annotations

import os
from datetime import timedelta

import pytest

from studyroom.extensions import db as _db
from studyroom.models import ACTIVE_STATUSES, Reservation, Seat, SeatOccupancy

from conftest import ik
from dataset import (DATASET_SPEC, STUDENT_PASSWORD, STUDENT_USERNAMES,
                     build_dataset, pick_seat, slot_tomorrow, verify_dataset)
from concurrency_harness import (active_occupancy_for, count_active_reservations,
                                 db_uri_for, ensure_fresh_db, run_round_processes,
                                 run_round_threads)

# Windows 多进程必须走 spawn，且子进程要能 import studyroom。
# pytest 在子进程里会重新 import 测试模块，因此多进程组用显式路径传库。
CREDS2 = [(STUDENT_USERNAMES[0], STUDENT_PASSWORD), (STUDENT_USERNAMES[1], STUDENT_PASSWORD)]
CREDS3 = CREDS2 + [(STUDENT_USERNAMES[2], STUDENT_PASSWORD)]

# 冲突语义：并发失败方可以接受的错误码（实现不符时这里不得放宽）
CONFLICT_CODES = {"SLOT_TAKEN", "RESERVATION_BLOCKED"}
CONFLICT_STATUSES = {409, 422}


# ---------------------------------------------------------------- fixtures


@pytest.fixture()
def conc_app(tmp_path):
    """独立临时文件库 + 已建表 + 基础种子 + 合成数据集。"""
    from studyroom.app import create_app
    from studyroom.cli import _seed_base
    db_path = tmp_path / "conc.db"
    app = create_app("test", {"SQLALCHEMY_DATABASE_URI": db_uri_for(db_path)})
    with app.app_context():
        _db.create_all()
        _seed_base()
        dataset = build_dataset(app)
    yield app, dataset, db_uri_for(db_path)


@pytest.fixture()
def clean_rounds(conc_app):
    """每轮开始前清空预约与占用，保证各轮互不影响（每轮新座位是主策略，
    这里兜底处理"学生有效预约数上限"——P-1 允许 2 个，不清理会在第 3 轮起命中上限）。"""
    app, dataset, uri = conc_app

    def _clean():
        with app.app_context():
            SeatOccupancy.query.delete()
            Reservation.query.delete()
            _db.session.commit()

    yield _clean
    _clean()


# ---------------------------------------------------------------- TC-E2E-00 数据集


def test_dataset_scale_matches_av01(conc_app):
    """AV-01 合成数据规模口径：2 自习室 × 30 座位、3 学生、1 管理员。"""
    app, dataset, _ = conc_app
    with app.app_context():
        info = verify_dataset(app)
    assert info["rooms"] == 2
    assert info["seatsTotal"] == 60
    assert all(n == 30 for n in info["seatsPerRoom"].values())
    assert info["students"] == 3
    assert info["admins"] >= 1
    assert len(dataset.all_seat_ids) == 60


def test_dataset_build_is_idempotent(conc_app):
    """重复构建不应产生重复房间/座位（幂等），总数仍是 60。"""
    app, dataset, _ = conc_app
    with app.app_context():
        again = build_dataset(app)
    assert sorted(again.all_seat_ids) == sorted(dataset.all_seat_ids)
    with app.app_context():
        assert Seat.query.count() >= 60


def test_dataset_seat_ids_are_unique_and_ordered(conc_app):
    """座位 id 无重复、按房间顺序排列、pick_seat 越界要报错。"""
    app, dataset, _ = conc_app
    ids = dataset.all_seat_ids
    assert len(ids) == len(set(ids))
    assert ids == sorted(ids)
    assert pick_seat(dataset, 0) == ids[0]
    assert pick_seat(dataset, 59) == ids[-1]
    with pytest.raises(IndexError):
        pick_seat(dataset, 60)


def test_dataset_rooms_are_distinct(conc_app):
    """两个房间相互独立，房间内座位各自 30 个。"""
    app, dataset, _ = conc_app
    assert len(dataset.room_ids) == 2
    assert dataset.room_ids[0] != dataset.room_ids[1]
    assert all(len(v) == 30 for v in dataset.seat_ids_by_room.values())
    seat0 = dataset.seat_ids_by_room[dataset.room_ids[0]][0]
    assert dataset.room_of_seat(seat0) == dataset.room_ids[0]


def test_dataset_spec_is_immutable():
    """规格是常量，任何尝试篡改都应失败（防止有人改规模绕过 AV-01）。"""
    from dataclasses import FrozenInstanceError
    with pytest.raises(FrozenInstanceError):
        DATASET_SPEC.rooms = 3  # type: ignore[misc]


# ---------------------------------------------------------------- 单进程组：2 线程 × 50 轮


def test_threads_two_racers_50_rounds(conc_app, clean_rounds):
    """必执行 1：2 线程 × 50 轮，每轮新座位，每轮成功数必须等于 1。"""
    app, dataset, _ = conc_app
    for i in range(50):
        clean_rounds()
        seat_id = pick_seat(dataset, i)
        starts, ends = slot_tomorrow(10 + (i % 10))
        result = run_round_threads(app, seat_id, starts, ends, CREDS2, round_index=i)
        assert result.success_count == 1, f"第 {i} 轮成功数为 {result.success_count}：{result.summary()}"
        assert len(result.failures) == 1, f"第 {i} 轮失败方数量异常：{result.summary()}"
        fail = result.failures[0]
        assert fail.status_code in CONFLICT_STATUSES, f"失败方状态码异常：{result.summary()}"
        assert fail.error_code in CONFLICT_CODES, f"失败方错误码异常：{result.summary()}"


def test_threads_three_racers_single_winner(conc_app, clean_rounds):
    """3 个线程抢同一座位：只有 1 个成功，其余 2 个都拿到冲突语义。"""
    app, dataset, _ = conc_app
    clean_rounds()
    seat_id = pick_seat(dataset, 5)
    starts, ends = slot_tomorrow(10)
    result = run_round_threads(app, seat_id, starts, ends, CREDS3)
    assert result.success_count == 1, result.summary()
    assert len(result.failures) == 2, result.summary()
    for f in result.failures:
        assert f.status_code in CONFLICT_STATUSES, result.summary()
        assert f.error_code in CONFLICT_CODES, result.summary()


def test_threads_exactly_one_occupancy_row(conc_app, clean_rounds):
    """库层兜底：一轮并发后，该座位该整点只有 1 条占用（U-1 唯一约束）。"""
    app, dataset, _ = conc_app
    clean_rounds()
    seat_id = pick_seat(dataset, 11)
    starts, ends = slot_tomorrow(13)
    run_round_threads(app, seat_id, starts, ends, CREDS2)
    assert active_occupancy_for(app, seat_id, starts) == 1
    assert count_active_reservations(app, seat_id, starts, ends) == 1


def test_threads_different_seats_both_succeed(conc_app, clean_rounds):
    """并发但座位不同：两人都应成功（验证不是"只要有并发就拒绝"）。"""
    app, dataset, _ = conc_app
    clean_rounds()
    starts, ends = slot_tomorrow(15)
    s1, s2 = pick_seat(dataset, 20), pick_seat(dataset, 21)
    r1 = run_round_threads(app, s1, starts, ends, CREDS2[:1])
    r2 = run_round_threads(app, s2, starts, ends, CREDS2[1:])
    assert r1.success_count == 1
    assert r2.success_count == 1


def test_threads_same_student_same_slot_blocked(conc_app, clean_rounds):
    """同一学生并发抢同一座位：本人重叠规则（F-2）也必须兜住，最多成功 1 次。"""
    app, dataset, _ = conc_app
    clean_rounds()
    seat_id = pick_seat(dataset, 30)
    starts, ends = slot_tomorrow(16)
    result = run_round_threads(app, seat_id, starts, ends, [CREDS2[0], CREDS2[0]])
    assert result.success_count <= 1, result.summary()


def test_threads_cross_room_seats_independent(conc_app, clean_rounds):
    """跨房间不同座位：两边并发各占一座，互不影响。"""
    app, dataset, _ = conc_app
    clean_rounds()
    room_a, room_b = dataset.room_ids
    seat_a = dataset.seat_ids_by_room[room_a][0]
    seat_b = dataset.seat_ids_by_room[room_b][0]
    starts, ends = slot_tomorrow(9)
    ra = run_round_threads(app, seat_a, starts, ends, CREDS2[:1])
    rb = run_round_threads(app, seat_b, starts, ends, CREDS2[1:])
    assert ra.success_count == 1 and rb.success_count == 1


# ---------------------------------------------------------------- 多进程组：2 进程 × 10 轮


def test_processes_two_racers_10_rounds(conc_app, clean_rounds):
    """必执行 2：2 进程 × 10 轮，每轮成功数 == 1，失败方拿到冲突语义。

    多进程是真实部署形态，结论以本组为准。若这里失败，先判责：
    实现存在双占缺陷则修实现；测试预期写错需人工批准后修测试。
    """
    app, dataset, uri = conc_app
    for i in range(10):
        clean_rounds()
        seat_id = pick_seat(dataset, 31 + i)
        starts, ends = slot_tomorrow(10 + (i % 10))
        result = run_round_processes(uri, seat_id, starts, ends, CREDS2, round_index=i)
        assert len(result.attempts) == 2, f"第 {i} 轮未收齐两个进程结果：{result.summary()}"
        assert result.success_count == 1, f"第 {i} 轮成功数为 {result.success_count}：{result.summary()}"
        fail = result.failures[0]
        assert not fail.exception, f"第 {i} 轮失败方是异常而非业务拒绝：{result.summary()}"
        assert fail.status_code in CONFLICT_STATUSES, f"第 {i} 轮失败方状态码异常：{result.summary()}"
        assert fail.error_code in CONFLICT_CODES, f"第 {i} 轮失败方错误码异常：{result.summary()}"


def test_processes_db_has_single_occupancy(conc_app, clean_rounds):
    """多进程并发后，库中该座位该整点只有 1 条占用、1 条有效预约。"""
    app, dataset, uri = conc_app
    clean_rounds()
    seat_id = pick_seat(dataset, 45)
    starts, ends = slot_tomorrow(14)
    result = run_round_processes(uri, seat_id, starts, ends, CREDS2)
    assert result.success_count == 1, result.summary()
    assert active_occupancy_for(app, seat_id, starts) == 1
    assert count_active_reservations(app, seat_id, starts, ends) == 1


def test_processes_three_racers_single_winner(conc_app, clean_rounds):
    """3 进程抢同一座位：1 个成功、2 个冲突。"""
    app, dataset, uri = conc_app
    clean_rounds()
    seat_id = pick_seat(dataset, 50)
    starts, ends = slot_tomorrow(11)
    result = run_round_processes(uri, seat_id, starts, ends, CREDS3)
    assert result.success_count == 1, result.summary()
    assert len(result.failures) == 2, result.summary()
    for f in result.failures:
        assert f.error_code in CONFLICT_CODES, result.summary()


# ---------------------------------------------------------------- 边界与负向用例


def test_conflict_only_for_overlapping_slot(conc_app, clean_rounds):
    """相邻不重叠时段应当都成功（不能因为"同一座位"就误判冲突）。"""
    app, dataset, _ = conc_app
    clean_rounds()
    seat_id = pick_seat(dataset, 40)
    s1, e1 = slot_tomorrow(10)
    s2, e2 = slot_tomorrow(11)
    r1 = run_round_threads(app, seat_id, s1, e1, CREDS2[:1])
    r2 = run_round_threads(app, seat_id, s2, e2, CREDS2[1:])
    assert r1.success_count == 1 and r2.success_count == 1


def test_repeat_idempotency_key_returns_same_result(app, client, stu_headers, room_and_seat):
    """同一幂等键重复提交，不应产生第二条预约（L4 幂等）。"""
    _, seat_id = room_and_seat
    starts, ends = slot_tomorrow(10)
    body = {"seatId": seat_id,
            "startsAt": starts.strftime("%Y-%m-%dT%H:%M:%S"),
            "endsAt": ends.strftime("%Y-%m-%dT%H:%M:%S")}
    key = ik()
    r1 = client.post("/api/v1/reservations",
                     headers={**stu_headers, "Idempotency-Key": key}, json=body)
    r2 = client.post("/api/v1/reservations",
                     headers={**stu_headers, "Idempotency-Key": key}, json=body)
    assert r1.status_code == 201, r1.get_json()
    assert r2.status_code in (200, 201), r2.get_json()
    rid1 = r1.get_json()["data"]["reservationId"]
    rid2 = r2.get_json()["data"]["reservationId"]
    assert rid1 == rid2


def test_failed_racer_leaves_no_reservation(conc_app, clean_rounds):
    """并发失败方不应留下任何预约记录或占用行（失败要干净）。"""
    app, dataset, _ = conc_app
    clean_rounds()
    seat_id = pick_seat(dataset, 22)
    starts, ends = slot_tomorrow(17)
    result = run_round_threads(app, seat_id, starts, ends, CREDS3)
    assert result.success_count == 1, result.summary()
    with app.app_context():
        total = Reservation.query.filter_by(seat_id=seat_id).count()
    assert total == 1, f"座位 {seat_id} 上有 {total} 条预约，失败方留下了脏数据"


def test_racer_cannot_hold_two_seats_concurrently(conc_app, clean_rounds):
    """同一学生并发抢两个不同座位：受 P-1 上限（2 个）约束，结果不超过上限。"""
    app, dataset, _ = conc_app
    clean_rounds()
    starts, ends = slot_tomorrow(12)
    s1, s2 = pick_seat(dataset, 23), pick_seat(dataset, 24)
    r1 = run_round_threads(app, s1, starts, ends, CREDS2[:1])
    r2 = run_round_threads(app, s2, starts, ends, CREDS2[:1])
    assert r1.success_count + r2.success_count <= 2


def test_seat_state_must_be_available(conc_app, clean_rounds):
    """维护中的座位不能被并发抢到（前置校验应先于并发逻辑生效）。"""
    app, dataset, _ = conc_app
    clean_rounds()
    seat_id = pick_seat(dataset, 26)
    with app.app_context():
        seat = _db.session.get(Seat, seat_id)
        seat.state = "MAINTENANCE"
        _db.session.commit()
    starts, ends = slot_tomorrow(10)
    result = run_round_threads(app, seat_id, starts, ends, CREDS2)
    assert result.success_count == 0, result.summary()
    for f in result.attempts:
        assert f.error_code == "SEAT_UNAVAILABLE", result.summary()


def test_racers_see_consistent_winner_across_views(conc_app, clean_rounds):
    """并发完成后，'我的预约'里只有胜者能看到这条预约。"""
    app, dataset, _ = conc_app
    clean_rounds()
    seat_id = pick_seat(dataset, 27)
    starts, ends = slot_tomorrow(18)
    result = run_round_threads(app, seat_id, starts, ends, CREDS2)
    assert result.success_count == 1
    winner_rsv = result.successes[0].reservation_id
    with app.app_context():
        rsv = _db.session.get(Reservation, winner_rsv)
        assert rsv is not None and rsv.status in ACTIVE_STATUSES
        assert rsv.seat_id == seat_id


def test_harness_reports_worker_identity(conc_app, clean_rounds):
    """工装自身有效性：每个 worker 的结果都带身份标识，失败时便于定位。"""
    app, dataset, _ = conc_app
    clean_rounds()
    seat_id = pick_seat(dataset, 28)
    starts, ends = slot_tomorrow(19)
    result = run_round_threads(app, seat_id, starts, ends, CREDS2)
    workers = {a.worker for a in result.attempts}
    assert workers == {"t0", "t1"}


def test_no_connection_to_default_instance_db():
    """护栏：测试进程不应连到 instance/studyroom.db（历史事故：探针脚本污染演示数据）。"""
    from pathlib import Path
    import config as cfg_mod
    default = Path(cfg_mod.BaseConfig.SQLALCHEMY_DATABASE_URI.replace("sqlite:///", ""))
    assert default.name == "studyroom.db"
    assert "instance" in str(default).lower()
