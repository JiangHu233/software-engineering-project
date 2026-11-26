from __future__ import annotations

import re
import time
from datetime import datetime

from ..extensions import db
from ..models import RuleParameter
from .timeutil import now, parse_date

# §5.8/§4.19：11 个规则参数的默认值与取值范围（判定一律读当前生效版本，禁止硬编码 D-10）
DEFAULT_PARAMS: dict[str, object] = {
    "bookingWindowDays": 7,
    "cancelLeadMinutes": 30,
    "checkInWindowMinutes": 15,
    "noCheckoutGraceMinutes": 15,
    "earlyLeaveThresholdMinutes": 30,
    "violationScoreThreshold": 3,
    "blacklistDaysPerEntry": 7,
    "blacklistMaxDays": 30,
    "maxActiveReservations": 2,
    "semesterStart": "2026-09-01",
    "tokenTtlHours": 8,
}

INT_PARAM_RANGES: dict[str, tuple[int, int]] = {
    "bookingWindowDays": (1, 30),
    "cancelLeadMinutes": (0, 1440),
    "checkInWindowMinutes": (1, 60),
    "noCheckoutGraceMinutes": (0, 60),
    "earlyLeaveThresholdMinutes": (0, 120),
    "violationScoreThreshold": (1, 10),
    "blacklistDaysPerEntry": (1, 30),
    "blacklistMaxDays": (1, 90),
    "maxActiveReservations": (1, 10),
    "tokenTtlHours": (1, 24),
}

_CACHE_TTL_SECONDS = 5.0  # 进程内缓存上限（§5.8）
_cached_at: float = 0.0
_cached: tuple[int, dict] | None = None

_DATE_IN_REASON = re.compile(r"\d{4}-\d{2}-\d{2}")
_REASON_KEYWORDS = ("批准", "审批", "依据", "纪要", "文件", "生效", "变更", "调整")


def invalidate_cache() -> None:
    global _cached, _cached_at
    _cached = None
    _cached_at = 0.0


def current() -> tuple[int, dict]:
    """当前生效参数 (version, params)。缓存 ≤5 秒；API-19 PUT 成功后主动失效。"""
    global _cached, _cached_at
    if _cached is not None and (time.monotonic() - _cached_at) < _CACHE_TTL_SECONDS:
        return _cached
    row = (RuleParameter.query
           .filter(RuleParameter.scope == "GLOBAL",
                   RuleParameter.effective_to.is_(None),
                   RuleParameter.effective_from <= now())
           .order_by(RuleParameter.version.desc())
           .first())
    if row is None:
        _cached = (1, dict(DEFAULT_PARAMS))
    else:
        params = dict(DEFAULT_PARAMS)
        params.update(row.params)
        _cached = (row.version, params)
    _cached_at = time.monotonic()
    return _cached


def current_row() -> RuleParameter | None:
    return (RuleParameter.query
            .filter(RuleParameter.scope == "GLOBAL",
                    RuleParameter.effective_to.is_(None),
                    RuleParameter.effective_from <= now())
            .order_by(RuleParameter.version.desc())
            .first())


def validate_update(incoming: dict) -> dict:
    """逐字段范围校验（§4.19）：越界/类型错/未知字段 → 422 RULE_PARAM_INVALID。"""
    errors: dict[str, list[str]] = {}
    for key, value in incoming.items():
        if key in INT_PARAM_RANGES:
            lo, hi = INT_PARAM_RANGES[key]
            if not isinstance(value, int) or isinstance(value, bool) or not (lo <= value <= hi):
                errors[key] = [f"取值必须是 {lo}~{hi} 的整数"]
        elif key == "semesterStart":
            if not isinstance(value, str):
                errors[key] = ["必须是 YYYY-MM-DD 字符串"]
            else:
                try:
                    parse_date(value, key)
                except Exception:
                    errors[key] = ["必须是 YYYY-MM-DD 字符串"]
        else:
            errors[key] = ["未知参数"]
    from ..core.errors import ApiError
    if errors:
        raise ApiError("RULE_PARAM_INVALID", fields=errors)


def validate_reason(reason: str) -> None:
    """reason 四项内容启发式校验（Q-25 裁决 C）：审批依据/批准人/生效日期/前后值。
    机器无法严格校验语义，采用：含日期串 + 长度下限 + 关键词命中数。"""
    from ..core.errors import validation_failed
    if not reason or not reason.strip():
        raise validation_failed({"reason": ["reason 必填"]})
    if not _DATE_IN_REASON.search(reason):
        raise validation_failed({"reason": ["reason 须包含生效日期（YYYY-MM-DD）"]})
    if len(reason.strip()) < 20:
        raise validation_failed({"reason": ["reason 须包含审批依据、批准人、生效日期与变更前后值"]})
    hits = sum(1 for kw in _REASON_KEYWORDS if kw in reason)
    if hits < 2:
        raise validation_failed({"reason": ["reason 须体现审批依据与批准人等信息"]})
