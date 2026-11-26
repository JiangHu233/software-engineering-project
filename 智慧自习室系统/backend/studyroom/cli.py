from __future__ import annotations

"""CLI：flask init-db（建表+基础种子，幂等）/ seed-demo（演示数据）/ run-scan（手动触发任务）。"""
import click
from flask import Flask, current_app

from .extensions import db
from .models import (Room, Seat, RuleParameter, User, ZoneDictionary)
from .services import rule_params
from .services.timeutil import today


def _seed_base() -> None:
    """基础种子（幂等）：区域字典 + 规则参数 v1 + 初始管理员。"""
    if ZoneDictionary.query.count() == 0:
        db.session.add_all([
            ZoneDictionary(code="A", label="A区", active=True),
            ZoneDictionary(code="B", label="B区", active=True),
            ZoneDictionary(code="SILENT", label="静音区", active=True),
            ZoneDictionary(code="WINDOW", label="靠窗区", active=True),
        ])
    if RuleParameter.query.filter_by(scope="GLOBAL").count() == 0:
        row = RuleParameter(scope="GLOBAL", version=1, created_by=None,
                            reason="初始默认参数（init-db 预置）")
        params = dict(rule_params.DEFAULT_PARAMS)
        params["semesterStart"] = today().replace(month=9, day=1).isoformat()
        row.params = params
        db.session.add(row)
    if User.query.filter_by(role="ADM").count() == 0:
        admin = User(username="admin", display_name="自习室管理员", role="ADM")
        admin.set_password(current_app.config["ADMIN_INITIAL_PASSWORD"])
        db.session.add(admin)
    db.session.commit()


def _seed_demo() -> None:
    """演示数据（幂等）：演示学生 + 星河自习室 12 座 + 一条历史预约。"""
    _seed_base()
    from datetime import datetime, timedelta
    student = User.query.filter_by(username="student").first()
    if student is None:
        student = User(username="student", display_name="演示学生", role="STU")
        student.set_password("student123")
        db.session.add(student)
        db.session.commit()
    room = Room.query.filter_by(name="星河自习室").first()
    if room is None:
        room = Room(name="星河自习室", location="图书馆 3F · 东区", open_hour=8, close_hour=22)
        db.session.add(room)
        db.session.commit()
        for i in range(1, 13):
            seat = Seat(room_id=room.id, label=f"A{i:02d}",
                        zone="A区" if i % 2 else "靠窗区", state="AVAILABLE")
            seat.features = ["靠窗", "电源"] if i % 2 else ["安静区", "电源"]
            db.session.add(seat)
        db.session.commit()


def register(app: Flask) -> None:
    @app.cli.command("init-db")
    def init_db_command():
        """建全部表 + 基础种子（区域字典/规则参数 v1/初始 admin）。"""
        db.create_all()
        _seed_base()
        pwd_hint = current_app.config["ADMIN_INITIAL_PASSWORD"]
        click.echo(f"数据库已初始化。初始管理员：admin（密码来自 ADMIN_INITIAL_PASSWORD，当前值 {pwd_hint}）")

    @app.cli.command("seed-demo")
    def seed_demo_command():
        """演示数据：student/student123 + 星河自习室 12 座。测试库禁用。"""
        db.create_all()
        _seed_demo()
        click.echo("演示数据已写入：admin、student/student123、星河自习室 12 座。")

    @app.cli.command("run-scan")
    @click.option("--task", type=click.Choice(["all", "T-1", "T-2", "T-3", "T-4"]),
                  default="all", help="要触发的任务")
    def run_scan_command(task: str):
        """手动触发定时任务（演示与排障；与调度器同一批幂等实现）。"""
        from .tasks.scan_timeouts import scan_no_checkout, scan_no_show
        from .tasks.blacklist_expire import expire_blacklists
        from .tasks.semester_reminder import semester_reminder
        results = {}
        if task in ("all", "T-1"):
            results["T-1"] = scan_no_show()
        if task in ("all", "T-2"):
            results["T-2"] = scan_no_checkout()
        if task in ("all", "T-3"):
            results["T-3"] = expire_blacklists()
        if task in ("all", "T-4"):
            results["T-4"] = semester_reminder()
        click.echo(str(results))
