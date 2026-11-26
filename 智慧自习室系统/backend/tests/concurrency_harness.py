"""并发测试工装（TASK-S0-01）。

职责：把"多个并发客户端同时抢同一个座位"这件事变成可重复的调用，
并且**不绕过任何应用层路径**——每个线程/进程都走完整的 HTTP 路由、
自己的 Flask test_client、自己的数据库连接。

硬性约束（任务卡）
------------------
1. 必须是**真实文件库**：SQLite 内存库的并发语义与文件库不同，无法暴露双占。
2. **每个 worker 独立连接**：SQLite 连接不是线程安全的，共享连接会让测试
   变成串行，看不出并发问题。
3. **禁止用 unittest.mock 替换数据库会话**：一旦 mock，测的就是假实现。
4. 每个用例一个独立临时库，严禁连接 instance/studyroom.db。

为什么线程版可能"假并发"
------------------------
Flask test_client 在同一进程里跑，SQLite 写锁由 GIL 之外的驱动层负责，因此
线程版能暴露一部分竞态；但进程级竞态（真实部署形态）只有多进程能暴露。
两者都要跑，结论以多进程为准——这正是任务卡要求"至少一组多进程用例"的原因。
"""

from __future__ import annotations

import multiprocessing as mp
import os
import queue
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------- 结果结构


@dataclass
class AttemptResult:
    """一次并发抢座的结果。"""

    worker: str
    ok: bool
    status_code: int = 0
    error_code: str = ""
    reservation_id: int | None = None
    started_at: float = 0.0
    finished_at: float = 0.0
    exception: str = ""

    @property
    def duration(self) -> float:
        return self.finished_at - self.started_at


@dataclass
class RoundResult:
    """一轮并发的结果汇总。"""

    round_index: int
    seat_id: int
    attempts: list[AttemptResult] = field(default_factory=list)

    @property
    def successes(self) -> list[AttemptResult]:
        return [a for a in self.attempts if a.ok]

    @property
    def failures(self) -> list[AttemptResult]:
        return [a for a in self.attempts if not a.ok]

    @property
    def success_count(self) -> int:
        return len(self.successes)

    def summary(self) -> str:
        parts = []
        for a in self.attempts:
            if a.ok:
                parts.append(f"{a.worker}=201(rsv={a.reservation_id})")
            elif a.exception:
                parts.append(f"{a.worker}=EXC({a.exception[:40]})")
            else:
                parts.append(f"{a.worker}={a.status_code}({a.error_code})")
        return f"round={self.round_index} seat={self.seat_id} " + " ".join(parts)


# ---------------------------------------------------------------- 单次尝试


def attempt_booking(app, username: str, password: str, seat_id: int,
                    starts_at: datetime, ends_at: datetime,
                    worker: str = "w") -> AttemptResult:
    """在一个**独立的工作线程**里完成一次预约请求。

    这里显式新建 test_client：Flask 的 app_context 与 SQLAlchemy session 是
    thread-local 的，只有在新线程里创建请求上下文，才会拿到新连接。
    """
    res = AttemptResult(worker=worker, ok=False)
    res.started_at = time.perf_counter()
    try:
        client = app.test_client()
        login = client.post("/api/v1/auth/login",
                            json={"username": username, "password": password})
        if login.status_code != 200:
            res.status_code = login.status_code
            res.error_code = "LOGIN_FAILED"
            res.exception = str(login.get_json())[:200]
            return res
        token = login.get_json()["data"]["token"]
        headers = {"Authorization": f"Bearer {token}",
                   "Idempotency-Key": str(uuid.uuid4())}
        resp = client.post("/api/v1/reservations", headers=headers, json={
            "seatId": seat_id,
            "startsAt": starts_at.strftime("%Y-%m-%dT%H:%M:%S"),
            "endsAt": ends_at.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        res.status_code = resp.status_code
        body = resp.get_json() or {}
        if resp.status_code == 201:
            res.ok = True
            res.reservation_id = (body.get("data") or {}).get("reservationId")
        else:
            err = body.get("error") or {}
            res.error_code = err.get("code", "")
    except Exception as exc:  # 线程内异常必须带回主线程，不能吞掉
        res.exception = f"{type(exc).__name__}: {exc}"
        res.error_code = "EXCEPTION"
    finally:
        res.finished_at = time.perf_counter()
    return res


def run_round_threads(app, seat_id: int, starts_at: datetime, ends_at: datetime,
                      credentials: list[tuple[str, str]],
                      round_index: int = 0, barrier: bool = True) -> RoundResult:
    """用 len(credentials) 个线程同时抢同一个座位。

    barrier=True 时所有线程在发请求前对齐到同一时刻，尽量制造真实竞争。
    """
    result = RoundResult(round_index=round_index, seat_id=seat_id)
    gate = threading.Barrier(len(credentials)) if barrier else None
    slots: list[AttemptResult | None] = [None] * len(credentials)

    def worker(idx: int, username: str, password: str) -> None:
        if gate is not None:
            try:
                gate.wait(timeout=10)
            except threading.BrokenBarrierError:
                pass
        slots[idx] = attempt_booking(app, username, password, seat_id,
                                     starts_at, ends_at, worker=f"t{idx}")

    threads = [threading.Thread(target=worker, args=(i, u, p), daemon=True)
               for i, (u, p) in enumerate(credentials)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    result.attempts = [s for s in slots if s is not None]
    return result


# ---------------------------------------------------------------- 多进程

# 每个进程各自 create_app —— 这是关键：只有独立进程才有独立的 SQLAlchemy 引擎
# 与独立 SQLite 连接，才能模拟真实部署下"两个用户同时提交"的情形。
_PROC_APP = None
_PROC_DB_URI = None


def _proc_init(db_uri: str) -> None:
    global _PROC_APP, _PROC_DB_URI
    from studyroom.app import create_app
    _PROC_APP = create_app("test", {"SQLALCHEMY_DATABASE_URI": db_uri})
    _PROC_DB_URI = db_uri


def _proc_worker(seat_id: int, starts_at_iso: str, ends_at_iso: str,
                 username: str, password: str, worker: str,
                 start_at: float, db_uri: str, out: "mp.Queue") -> None:
    global _PROC_APP, _PROC_DB_URI
    try:
        # spawn 出来的进程不继承父进程全局变量，必须自己建 app 与引擎
        if _PROC_APP is None:
            from studyroom.app import create_app
            _PROC_APP = create_app("test", {"SQLALCHEMY_DATABASE_URI": db_uri})
            _PROC_DB_URI = db_uri
        # 自旋到统一发车时刻，尽量让两个进程真正同时提交
        while time.time() < start_at:
            time.sleep(0.001)
        starts_at = datetime.fromisoformat(starts_at_iso)
        ends_at = datetime.fromisoformat(ends_at_iso)
        res = attempt_booking(_PROC_APP, username, password, seat_id,
                              starts_at, ends_at, worker=worker)
        out.put({
            "worker": res.worker, "ok": res.ok, "status_code": res.status_code,
            "error_code": res.error_code, "reservation_id": res.reservation_id,
            "exception": res.exception,
        })
    except Exception as exc:
        out.put({"worker": worker, "ok": False, "status_code": 0,
                 "error_code": "PROC_EXCEPTION",
                 "exception": traceback.format_exc(limit=3)})


def run_round_processes(db_uri: str, seat_id: int, starts_at: datetime,
                        ends_at: datetime, credentials: list[tuple[str, str]],
                        round_index: int = 0, lead_seconds: float = 0.35,
                        timeout: float = 90.0) -> RoundResult:
    """用 len(credentials) 个独立进程同时抢同一个座位。"""
    result = RoundResult(round_index=round_index, seat_id=seat_id)
    ctx = mp.get_context("spawn")  # spawn 才能真正独立（fork 会继承父进程引擎）
    out: "mp.Queue" = ctx.Queue()
    start_at = time.time() + lead_seconds

    procs = []
    for idx, (username, password) in enumerate(credentials):
        p = ctx.Process(target=_proc_worker,
                        args=(seat_id, starts_at.isoformat(), ends_at.isoformat(),
                              username, password, f"p{idx}", start_at, db_uri, out))
        p.start()
        procs.append(p)

    deadline = time.time() + timeout
    collected = 0
    while collected < len(procs) and time.time() < deadline:
        try:
            payload = out.get(timeout=1.0)
        except queue.Empty:
            continue
        collected += 1
        result.attempts.append(AttemptResult(
            worker=payload["worker"], ok=payload["ok"],
            status_code=payload["status_code"], error_code=payload["error_code"],
            reservation_id=payload.get("reservation_id"),
            exception=payload.get("exception", ""),
        ))

    for p in procs:
        p.join(timeout=15)
        if p.is_alive():
            p.terminate()
            p.join(timeout=5)

    return result


# ---------------------------------------------------------------- 库与断言辅助


def make_workdir(tmp_path: Path, name: str = "conc") -> Path:
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_uri_for(path: Path) -> str:
    return f"sqlite:///{path}"


def active_occupancy_for(app, seat_id: int, slot_start: datetime) -> int:
    """直接查库：该座位该整点有几条有效占用（U-1 的唯一性应由库兜底）。"""
    from studyroom.extensions import db
    from studyroom.models import SeatOccupancy
    with app.app_context():
        return (SeatOccupancy.query
                .filter(SeatOccupancy.seat_id == seat_id,
                        SeatOccupancy.slot_start == slot_start)
                .count())


def count_active_reservations(app, seat_id: int, starts_at: datetime,
                              ends_at: datetime) -> int:
    """直接查库：与该时段重叠的有效预约条数（应恒为 1）。"""
    from studyroom.extensions import db
    from studyroom.models import ACTIVE_STATUSES, Reservation
    with app.app_context():
        return (Reservation.query
                .filter(Reservation.seat_id == seat_id,
                        Reservation.status.in_(ACTIVE_STATUSES),
                        Reservation.starts_at < ends_at,
                        Reservation.ends_at > starts_at)
                .count())


def ensure_fresh_db(db_uri: str) -> None:
    """把 db_uri 指向的库删掉重建（spawn 出来的进程会用它初始化）。"""
    path = Path(db_uri[len("sqlite:///"):])
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            p.unlink()


assert os.name == "nt"  # 本工装按 Windows 多进程语义编写；跨平台需复核 spawn 行为
