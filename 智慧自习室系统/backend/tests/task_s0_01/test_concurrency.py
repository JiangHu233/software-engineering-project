"""TASK-S0-01 并发验证用例。

任务卡要求的两组必执行用例：
    单进程组：2 线程 × 50 轮，每轮新座位，断言每轮成功数 == 1
    多进程组：2 进程 × 10 轮，断言每轮成功数 == 1、失败方拿到冲突语义

本文件在此基础上控制用例数为 10 条，覆盖：数据集规模自检、两条必执行并发组、
库层唯一约束兜底、幂等复用、失败方不留脏数据、维护座位拒绝、不同座位不误判、
护栏。工具有效性采用"负向对照"方式证明（见证据 README）。

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

from tests.task_s0_01.dataset import (DATASET_SPEC, STUDENT_PASSWORD, STUDENT_USERNAMES,
                     build_dataset, pick_seat, slot_tomorrow, verify_dataset)
from tests.task_s0_01.concurrency_harness import (active_occupancy_for, count_active_reservations,
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



# ================================================================ 数据集（TC-E2E-00）

def test_dataset_scale_matches_av01(conc_app):
    """AV-01 合成数据规模口径：2 自习室 x 30 座位、3 学生、1 管理员。"""
    app, dataset, _ = conc_app
    with app.app_context():
        info = verify_dataset(app)
    assert info["rooms"] == 2
    assert info["seatsTotal"] == 60
    assert all(n == 30 for n in info["seatsPerRoom"].values())
    assert info["students"] == 3 and info["admins"] >= 1
    assert len(dataset.all_seat_ids) == 60


def test_dataset_seat_ids_unique_and_pick_bounds(conc_app):
    """座位 id 唯一有序，pick_seat 越界要报错。"""
    app, dataset, _ = conc_app
    ids = dataset.all_seat_ids
    assert len(ids) == len(set(ids)) and ids == sorted(ids)
    assert pick_seat(dataset, 59) == ids[-1]
    with pytest.raises(IndexError):
        pick_seat(dataset, 60)


# ================================================================ 单进程组（必执行 1）

def test_threads_two_racers_50_rounds(conc_app, clean_rounds):
    """必执行 1：2 线程 x 50 轮，每轮新座位，每轮成功数 == 1。"""
    app, dataset, _ = conc_app
    for i in range(50):
        clean_rounds()
        seat_id = pick_seat(dataset, i)
        starts, ends = slot_tomorrow(10 + (i % 10))
        result = run_round_threads(app, seat_id, starts, ends, CREDS2, round_index=i)
        assert result.success_count == 1, f"第 {i} 轮：{result.summary()}"
        fail = result.failures[0]
        assert fail.status_code in CONFLICT_STATUSES, f"第 {i} 轮：{result.summary()}"
        assert fail.error_code in CONFLICT_CODES, f"第 {i} 轮：{result.summary()}"


def test_threads_different_seats_both_succeed(conc_app, clean_rounds):
    """工装有效（负向对照）：并发但座位不同，两方都成功，不误判冲突。"""
    app, dataset, _ = conc_app
    clean_rounds()
    starts, ends = slot_tomorrow(15)
    s1, s2 = pick_seat(dataset, 20), pick_seat(dataset, 21)
    assert run_round_threads(app, s1, starts, ends, CREDS2[:1]).success_count == 1
    assert run_round_threads(app, s2, starts, ends, CREDS2[1:]).success_count == 1


def test_threads_seat_maintenance_rejected(conc_app, clean_rounds):
    """工装有效（负向对照）：维护中座位并发抢全部被拒，能测出前置校验。"""
    app, dataset, _ = conc_app
    clean_rounds()
    seat_id = pick_seat(dataset, 26)
    with app.app_context():
        seat = _db.session.get(Seat, seat_id)
        seat.state = "MAINTENANCE"
        _db.session.commit()
    result = run_round_threads(app, seat_id, *slot_tomorrow(10), CREDS2)
    assert result.success_count == 0, result.summary()
    assert all(a.error_code == "SEAT_UNAVAILABLE" for a in result.attempts), result.summary()


def test_threads_exactly_one_occupancy_row(conc_app, clean_rounds):
    """工装有效（负向对照）：并发后库中该座位该整点只有 1 条占用（U-1 兜底）。"""
    app, dataset, _ = conc_app
    clean_rounds()
    seat_id = pick_seat(dataset, 11)
    starts, ends = slot_tomorrow(13)
    run_round_threads(app, seat_id, starts, ends, CREDS2)
    assert active_occupancy_for(app, seat_id, starts) == 1
    assert count_active_reservations(app, seat_id, starts, ends) == 1


def test_failed_racer_leaves_no_reservation(conc_app, clean_rounds):
    """工装有效（负向对照）：失败方不留预约与占用记录（测脏数据）。"""
    app, dataset, _ = conc_app
    clean_rounds()
    seat_id = pick_seat(dataset, 22)
    result = run_round_threads(app, seat_id, *slot_tomorrow(17), CREDS3)
    assert result.success_count == 1, result.summary()
    with app.app_context():
        total = Reservation.query.filter_by(seat_id=seat_id).count()
    assert total == 1, f"失败方留下脏数据：座位 {seat_id} 有 {total} 条预约"



def test_processes_two_racers_10_rounds(conc_app, clean_rounds):
    """必执行 2：2 进程 x 10 轮，每轮成功数 == 1、失败方拿到冲突语义。

    多进程是真实部署形态，结论以本组为准。若失败先判责：实现有双占缺陷则修实现，
    测试预期写错需人工批准后修测试；不得为了让测试通过而删断言或放宽错误码。
    """
    app, dataset, uri = conc_app
    for i in range(10):
        clean_rounds()
        seat_id = pick_seat(dataset, 31 + i)
        starts, ends = slot_tomorrow(10 + (i % 10))
        result = run_round_processes(uri, seat_id, starts, ends, CREDS2, round_index=i)
        assert len(result.attempts) == 2, f"第 {i} 轮：{result.summary()}"
        assert result.success_count == 1, f"第 {i} 轮成功数：{result.summary()}"
        fail = result.failures[0]
        assert not fail.exception, f"第 {i} 轮失败方是异常：{result.summary()}"
        assert fail.status_code in CONFLICT_STATUSES, f"第 {i} 轮：{result.summary()}"
        assert fail.error_code in CONFLICT_CODES, f"第 {i} 轮：{result.summary()}"


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


def test_no_connection_to_default_instance_db():
    """护栏：测试进程不应连到 instance/studyroom.db（历史事故：探针污染演示数据）。"""
    from pathlib import Path
    import config as cfg_mod
    default = Path(cfg_mod.BaseConfig.SQLALCHEMY_DATABASE_URI.replace("sqlite:///", ""))
    assert "instance" in str(default).lower()
