from __future__ import annotations

from flask import Flask, current_app, g, jsonify
from werkzeug.exceptions import HTTPException
from sqlalchemy.exc import IntegrityError

from .errors import ApiError

# IntegrityError 按唯一约束涉及列映射（SQLite 报文含 "UNIQUE constraint failed: tbl.col, ..."）
_UNIQUE_COLUMN_MAP: dict[frozenset[str], tuple[str, dict]] = {
    frozenset({"seat_occupancy.seat_id", "seat_occupancy.slot_start"}):
        ("SLOT_TAKEN", {"reason": "OCCUPIED"}),
    frozenset({"violation.reservation_id"}):
        ("DUPLICATE_VIOLATION", {}),
    frozenset({"seat.room_id", "seat.label"}):
        ("SEAT_LABEL_DUPLICATED", {}),
    frozenset({"room.name"}):
        ("ROOM_NAME_DUPLICATED", {}),
    frozenset({"idempotency_record.actor_id", "idempotency_record.method",
               "idempotency_record.path", "idempotency_record.key"}):
        ("DUPLICATE_REQUEST", {}),
}


def _error_body(code: str, message: str, fields: dict, rule: str | None) -> dict:
    return {"error": {
        "code": code, "message": message, "fields": fields, "rule": rule,
        "requestId": getattr(g, "request_id", None),
    }}


def _map_integrity_error(exc: IntegrityError) -> ApiError:
    msg = str(exc.orig) if exc.orig else str(exc)
    if "UNIQUE constraint failed" in msg:
        cols_part = msg.split("UNIQUE constraint failed:", 1)[1]
        cols = frozenset(c.strip() for c in cols_part.split(",") if c.strip())
        for known, (code, fields) in _UNIQUE_COLUMN_MAP.items():
            if known <= cols or cols <= known:
                return ApiError(code, fields=fields)
    if "FOREIGN KEY constraint failed" in msg:
        return ApiError("VALIDATION_FAILED", fields={"fk": ["引用的对象不存在"]})
    return ApiError("INTERNAL_ERROR")


def install(app: Flask) -> None:
    @app.errorhandler(ApiError)
    def _handle_api_error(err: ApiError):
        return jsonify(_error_body(err.code, err.message, err.fields, err.rule)), err.http_status

    @app.errorhandler(IntegrityError)
    def _handle_integrity(err: IntegrityError):
        mapped = _map_integrity_error(err)
        app.logger.warning("IntegrityError mapped to %s: %s", mapped.code, err.orig)
        return jsonify(_error_body(mapped.code, mapped.message, mapped.fields, mapped.rule)), mapped.http_status

    @app.errorhandler(HTTPException)
    def _handle_http(exc: HTTPException):
        # 404/405 等 Flask 原生异常 → 统一错误包；404 映射 NOT_FOUND，其余保持语义
        code = "NOT_FOUND" if exc.code == 404 else (
            "VALIDATION_FAILED" if exc.code == 400 else "INTERNAL_ERROR"
        )
        if exc.code == 404:
            message = "目标资源不存在。"
        elif exc.code == 400:
            message = "请求格式非法。"
        else:
            message = exc.description or "请求处理失败。"
        return jsonify(_error_body(code, message, {}, None)), exc.code or 500

    @app.errorhandler(Exception)
    def _handle_unexpected(exc: Exception):
        app.logger.exception("unhandled exception")
        err = ApiError("INTERNAL_ERROR")
        return jsonify(_error_body(err.code, err.message, {}, err.rule)), 500
