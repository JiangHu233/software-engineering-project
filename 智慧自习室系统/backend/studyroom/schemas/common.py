from __future__ import annotations

"""请求字段解析（camelCase 输入的唯一入口）：类型/长度/必填校验 → 400 VALIDATION_FAILED。"""
from flask import request

from ..core.errors import validation_failed


def get_json() -> dict:
    data = request.get_json(silent=True)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise validation_failed({"body": ["请求体必须为 JSON 对象"]})
    return data


def field_str(body: dict, name: str, *, required: bool = True, min_len: int = 1,
              max_len: int | None = None) -> str | None:
    value = body.get(name)
    if value is None or (isinstance(value, str) and value.strip() == ""):
        if required:
            raise validation_failed({name: [f"{name} 必填"]})
        return None
    if not isinstance(value, str):
        raise validation_failed({name: [f"{name} 必须为字符串"]})
    value = value.strip()
    if len(value) < min_len:
        raise validation_failed({name: [f"{name} 长度不得小于 {min_len}"]})
    if max_len is not None and len(value) > max_len:
        raise validation_failed({name: [f"{name} 长度不得超过 {max_len}"]})
    return value


def field_int(body: dict, name: str, *, required: bool = True,
              lo: int | None = None, hi: int | None = None) -> int | None:
    value = body.get(name)
    if value is None:
        if required:
            raise validation_failed({name: [f"{name} 必填"]})
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise validation_failed({name: [f"{name} 必须为整数"]})
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        raise validation_failed({name: [f"{name} 取值范围 {lo}~{hi}"]})
    return value


def field_str_list(body: dict, name: str, *, max_items: int, item_max_len: int) -> list[str]:
    value = body.get(name)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise validation_failed({name: [f"{name} 必须为字符串数组"]})
    if len(value) > max_items:
        raise validation_failed({name: [f"{name} 最多 {max_items} 个"]})
    for v in value:
        if not v.strip() or len(v) > item_max_len:
            raise validation_failed({name: [f"元素须非空且不超过 {item_max_len} 字符"]})
    return [v.strip() for v in value]


def query_bool(name: str) -> bool | None:
    raw = request.args.get(name)
    if raw is None:
        return None
    return raw.lower() in ("1", "true", "yes")
