from __future__ import annotations

from datetime import datetime, timedelta

from flask import request

from ..services.timeutil import parse_date, parse_iso_local
from .errors import validation_failed

MAX_PAGE_SIZE = 100
MAX_FROM_TO_DAYS = 31


def parse_pagination() -> tuple[int, int]:
    page = request.args.get("page", "1")
    page_size = request.args.get("pageSize", "20")
    try:
        page = int(page)
        page_size = int(page_size)
    except ValueError:
        raise validation_failed({"page": ["page/pageSize 必须为整数"]})
    if page < 1:
        raise validation_failed({"page": ["page 从 1 开始"]})
    if page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise validation_failed({"pageSize": [f"pageSize 取值 1~{MAX_PAGE_SIZE}"]})
    return page, page_size


def parse_from_to() -> tuple[datetime | None, datetime | None]:
    """from/to：接受 ISO 日期时间或裸日期（视为当日 00:00）；from<to 且跨度≤31 天（§3.5）。"""
    raw_from = request.args.get("from")
    raw_to = request.args.get("to")
    if raw_from is None and raw_to is None:
        return None, None
    if raw_from is None or raw_to is None:
        raise validation_failed({"from": ["from/to 必须成对出现"]})
    dt_from = _parse_moment(raw_from, "from")
    dt_to = _parse_moment(raw_to, "to")
    if dt_from >= dt_to:
        raise validation_failed({"from": ["from 必须早于 to"]})
    if dt_to - dt_from > timedelta(days=MAX_FROM_TO_DAYS):
        raise validation_failed({"from": [f"from/to 跨度不得超过 {MAX_FROM_TO_DAYS} 天"]})
    return dt_from, dt_to


def _parse_moment(value: str, field: str) -> datetime:
    try:
        if len(value) == 10:
            return datetime.combine(parse_date(value, field), datetime.min.time())
        return parse_iso_local(value, field)
    except ApiError:
        raise
    except Exception:
        raise validation_failed({field: [f"{field} 格式非法"]})


def parse_enum_list(field: str, allowed: set[str]) -> list[str] | None:
    raw = request.args.get(field)
    if raw is None:
        return None
    values = [v.strip().upper() for v in raw.split(",") if v.strip()]
    if not values:
        return None
    bad = [v for v in values if v not in allowed]
    if bad:
        raise validation_failed({field: [f"不支持的取值：{','.join(bad)}，允许：{','.join(sorted(allowed))}"]})
    return values


def apply_sort(query, whitelist: dict[str, object], default: list[tuple[object, bool]],
               sort_param: str | None):
    """sort 白名单：field,asc|desc；末位强制补 id 保证唯一定序（§3.5）。禁止拼 SQL。"""
    order_cols = list(default)
    raw = sort_param
    if raw:
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if not parts:
            raise validation_failed({"sort": ["sort 格式为 field,asc 或 field,desc"]})
        field = parts[0]
        direction = parts[1].lower() if len(parts) > 1 else "asc"
        if field not in whitelist:
            raise validation_failed({"sort": [f"排序字段不在白名单：{field}"]})
        if direction not in ("asc", "desc"):
            raise validation_failed({"sort": ["方向只允许 asc/desc"]})
        col = whitelist[field]
        from sqlalchemy import asc, desc
        order_cols = [(col, direction == "desc")]
    from sqlalchemy import asc, desc
    for col, is_desc in order_cols:
        query = query.order_by(desc(col) if is_desc else asc(col))
    return query
