from __future__ import annotations

"""API-20 运维脱敏指标：http 内存环形计数 + business 标量 SQL 计数，无任何个人字段。"""
from datetime import datetime

from ..core.request_context import http_metrics
from ..extensions import db
from ..models import ACTIVE_STATUSES, RSV_CHECKED_IN, Reservation, Violation
from .timeutil import now

_WINDOW_MINUTES = {"1m": 1, "5m": 5, "1h": 60}


def metrics(window: str) -> dict:
    minutes = _WINDOW_MINUTES[window]
    t_now = now()
    today_start = datetime.combine(t_now.date(), datetime.min.time())
    active = Reservation.query.filter(Reservation.status.in_(ACTIVE_STATUSES)).count()
    checked_in = Reservation.query.filter(
        Reservation.status == RSV_CHECKED_IN,
        Reservation.starts_at <= t_now, Reservation.ends_at > t_now).count()
    violations_today = Violation.query.filter(Violation.created_at >= today_start).count()
    return {
        "generatedAt": t_now.isoformat(timespec="seconds"),
        "http": http_metrics(minutes),
        "business": {
            "activeReservations": active,
            "checkedInNow": checked_in,
            "violationsToday": violations_today,
        },
    }
