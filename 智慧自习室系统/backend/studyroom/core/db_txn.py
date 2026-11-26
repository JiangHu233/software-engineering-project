from __future__ import annotations

from contextlib import contextmanager

from flask import current_app
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError

from ..extensions import db
from .errors import ApiError


def install_engine_pragmas(app) -> None:
    """连接级 PRAGMA：外键、busy_timeout、WAL（§5.4 SQLite 落地）。"""
    with app.app_context():
        engine = db.engine

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _record):  # pragma: no cover - 驱动层
        cur = dbapi_conn.cursor()
        cur.execute(f"PRAGMA busy_timeout={app.config['DB_BUSY_TIMEOUT_MS']}")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()


@contextmanager
def immediate_transaction():
    """写事务：BEGIN IMMEDIATE 全局写锁（Q-28 裁决 C），退出即提交，异常回滚。

    锁内代码必须毫秒级完成：禁止循环查询、禁止外部调用（§5.4 代价条款）。
    busy_timeout 超时抛 OperationalError("database is locked")，由 handlers 映射为
    409 SLOT_TAKEN / LOCK_TIMEOUT（Q-29 裁决 C）。
    """
    db.session.rollback()  # 丢弃此前隐式开启的读事务，保证 BEGIN 干净
    try:
        db.session.execute(text("BEGIN IMMEDIATE"))
    except OperationalError as exc:  # 极端：连接层即锁超时
        db.session.rollback()
        raise ApiError("SLOT_TAKEN", fields={"reason": "LOCK_TIMEOUT"}) from exc
    try:
        yield
        db.session.commit()
    except ApiError:
        db.session.rollback()
        raise
    except OperationalError as exc:
        db.session.rollback()
        if "locked" in str(exc.orig).lower():
            raise ApiError("SLOT_TAKEN", fields={"reason": "LOCK_TIMEOUT"}) from exc
        raise
    except Exception:
        db.session.rollback()
        raise
