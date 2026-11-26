from __future__ import annotations

from flask import g, jsonify


def _meta(**extra) -> dict:
    meta = {"requestId": getattr(g, "request_id", None)}
    meta.update(extra)
    return meta


def ok(data, status: int = 200, meta: dict | None = None, headers: dict | None = None):
    body = {"data": data, "meta": _meta(**(meta or {}))}
    resp = jsonify(body)
    resp.status_code = status
    for k, v in (headers or {}).items():
        resp.headers[k] = v
    return resp


def created(data, location: str, meta: dict | None = None):
    return ok(data, status=201, meta=meta, headers={"Location": location})


def page_meta(page: int, page_size: int, total: int, **extra) -> dict:
    total_pages = (total + page_size - 1) // page_size if page_size else 0
    return {
        "page": page,
        "pageSize": page_size,
        "total": total,
        "totalPages": total_pages,
        **extra,
    }
