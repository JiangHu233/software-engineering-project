from __future__ import annotations

from datetime import date, datetime, time, timedelta

from ..core.errors import validation_failed

DATE_FMT = "%Y-%m-%d"


def now() -> datetime:
    """业务时钟：服务器本地 naive 时间（AS-03）。判定只允许"表字段 vs now()"（Q-18）。"""
    return datetime.now()


def today() -> date:
    return datetime.now().date()


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.replace(microsecond=0).isoformat(sep="T")


def iso_date(d: date | None) -> str | None:
    return d.strftime(DATE_FMT) if d else None


def parse_iso_local(value: str, field: str) -> datetime:
    """解析 ISO-8601 本地时间；拒绝时区后缀（§3.1/裁定#9）。"""
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise validation_failed({field: [f"{field} 必须为 ISO-8601 本地时间，如 2026-12-01T14:00:00"]})
    if dt.tzinfo is not None:
        raise validation_failed({field: [f"{field} 不得携带时区后缀"]})
    return dt


def parse_date(value: str, field: str) -> date:
    try:
        return datetime.strptime(value, DATE_FMT).date()
    except (TypeError, ValueError):
        raise validation_failed({field: [f"{field} 必须为 YYYY-MM-DD"]})


def parse_hhmm(value: str, field: str) -> time:
    """HH:mm 整点；24:00 非法（§3.1：到当天结束用 to=23:59 或省略）。"""
    try:
        t = datetime.strptime(value, "%H:%M").time()
    except (TypeError, ValueError):
        raise validation_failed({field: [f"{field} 必须为 HH:mm 整点"]})
    if t.minute != 0 or t.hour > 23:
        raise validation_failed({field: [f"{field} 必须为整点且不得为 24:00"]})
    return t


def is_on_hour(dt: datetime) -> bool:
    return dt.minute == 0 and dt.second == 0 and dt.microsecond == 0


def effective_end_hour(ends_at: datetime) -> int:
    """endsAt 落在次日 T00:00 表示"到当天 24:00 关闭"（R-2），折算为 24。"""
    if ends_at.time() == time(0, 0):
        return 24
    return ends_at.hour


def slot_range(starts_at: datetime, ends_at: datetime) -> list[datetime]:
    """把 [startsAt, endsAt) 拆成整点格（ADR-002）。调用方保证两端整点且 start < end。"""
    slots = []
    cur = starts_at
    while cur < ends_at:
        slots.append(cur)
        cur += timedelta(hours=1)
    return slots


def combine(day: date, t: time) -> datetime:
    return datetime.combine(day, t)


def end_of_day(day: date) -> datetime:
    return datetime.combine(day + timedelta(days=1), time(0, 0))
