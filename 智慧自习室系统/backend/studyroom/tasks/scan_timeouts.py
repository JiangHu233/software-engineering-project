from __future__ import annotations

"""T-1 超时未签到 / T-2 超时未签退（§5.6）。

红线：判定只用"表字段 vs now()"（Q-18）；每条一个事务；条件更新先到者胜（Q-31）；
与用户请求竞争输掉（状态已被改）时静默跳过，不算失败。
"""
import time as _time
from datetime import datetime, timedelta

from ..extensions import db
from ..models import RSV_BOOKED, RSV_CHECKED_IN, Reservation, ScanRun, add_audit
from ..services import rule_params, violation_service
from ..core.db_txn import immediate_transaction
from ..core.errors import ApiError


def _run_scan(task_name: str, pickup, handler) -> dict:
    started = _time.monotonic()
    run = ScanRun(task_name=task_name, started_at=datetime.now(), status="RUNNING")
    with immediate_transaction():
        db.session.add(run)
        db.session.flush()
    scanned = succeeded = failed = 0
    for reservation in pickup():
        scanned += 1
        try:
            with immediate_transaction():
                # 重新加载拿最新状态：前一条事务提交后可能已被用户/其他任务处理
                fresh = db.session.get(Reservation, reservation.id)
                if fresh is None or fresh.status not in (RSV_BOOKED, RSV_CHECKED_IN):
                    continue  # 已被并发方处理（先到者胜）
                handler(fresh)
                succeeded += 1
        except ApiError as exc:
            if exc.code == "RESERVATION_STATE_CONFLICT":
                continue  # 状态竞争失败，跳过不中断（§5.6）
            failed += 1
            _audit_scan_failure(task_name, reservation.id, exc.code)
        except Exception:
            failed += 1
            _audit_scan_failure(task_name, reservation.id, "INTERNAL_ERROR")
    duration_ms = int((_time.monotonic() - started) * 1000)
    with immediate_transaction():
        run.finished_at = datetime.now()
        run.duration_ms = duration_ms
        run.scanned, run.succeeded, run.failed = scanned, succeeded, failed
        run.status = "DONE"
        db.session.add(run)
    return {"scanned": scanned, "succeeded": succeeded, "failed": failed,
            "durationMs": duration_ms}


def _audit_scan_failure(task_name: str, reservation_id: int, code: str) -> None:
    try:
        with immediate_transaction():
            add_audit("SCAN_ITEM_FAILED", target_type="reservation",
                      target_id=reservation_id, result_code=code)
    except Exception:
        pass


def pickup_no_show():
    _, params = rule_params.current()
    threshold = datetime.now() - timedelta(minutes=int(params["checkInWindowMinutes"]))
    # 条件 starts_at + checkInWindow < now 等价改写为 starts_at < now - checkInWindow（走 I-3）
    return (Reservation.query
            .filter(Reservation.status == RSV_BOOKED, Reservation.starts_at < threshold)
            .order_by(Reservation.id).all())


def pickup_no_checkout():
    _, params = rule_params.current()
    threshold = datetime.now() - timedelta(minutes=int(params["noCheckoutGraceMinutes"]))
    return (Reservation.query
            .filter(Reservation.status == RSV_CHECKED_IN, Reservation.ends_at < threshold)
            .order_by(Reservation.id).all())


def scan_no_show() -> dict:
    """T-1：BOOKED→VIOLATED + NO_SHOW（删全部占用/计分/黑名单/outbox，每条一事务）。"""
    def handler(reservation: Reservation) -> None:
        violation_service.record_violation(reservation, "NO_SHOW", on_duplicate="skip")
    return _run_scan("T-1_no_show", pickup_no_show, handler)


def scan_no_checkout() -> dict:
    """T-2：CHECKED_IN→VIOLATED + NO_CHECKOUT。"""
    def handler(reservation: Reservation) -> None:
        violation_service.record_violation(reservation, "NO_CHECKOUT", on_duplicate="skip")
    return _run_scan("T-2_no_checkout", pickup_no_checkout, handler)
