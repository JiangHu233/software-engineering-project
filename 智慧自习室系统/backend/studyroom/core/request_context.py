from __future__ import annotations

import time as _time
import uuid
from collections import deque
from threading import Lock

from flask import g, request

_metrics_lock = Lock()
# 每分钟桶：{minute_epoch: [requests, errors, latencies_ms_sum, count_for_p95...]}
# 简化实现：每分钟一个桶存 (requests, errors, latency 列表)
_buckets: dict[int, dict] = {}
_WINDOW_MINUTES = 61  # 覆盖 1h 查询窗口


def _bucket(minute: int) -> dict:
    with _metrics_lock:
        b = _buckets.get(minute)
        if b is None:
            b = {"requests": 0, "errors": 0, "latencies": []}
            _buckets[minute] = b
            # 清理过期桶
            if len(_buckets) > _WINDOW_MINUTES:
                cutoff = minute - _WINDOW_MINUTES
                for k in [k for k in _buckets if k < cutoff]:
                    del _buckets[k]
        return b


def record_http(duration_ms: float, status: int) -> None:
    minute = int(_time.time() // 60)
    b = _bucket(minute)
    with _metrics_lock:
        b["requests"] += 1
        if status >= 400:
            b["errors"] += 1
        b["latencies"].append(duration_ms)


def http_metrics(window_minutes: int) -> dict:
    now_minute = int(_time.time() // 60)
    requests = errors = 0
    latencies: list[float] = []
    with _metrics_lock:
        for m in range(now_minute - window_minutes + 1, now_minute + 1):
            b = _buckets.get(m)
            if b:
                requests += b["requests"]
                errors += b["errors"]
                latencies.extend(b["latencies"])
    latencies.sort()
    p95 = latencies[int(len(latencies) * 0.95) - 1] if latencies else 0
    return {"requests": requests, "errors": errors, "p95Ms": round(p95, 2)}


def install(app) -> None:
    @app.before_request
    def _assign_request_id():
        g.request_id = uuid.uuid4().hex[:16]
        g.request_started = _time.monotonic()

    @app.after_request
    def _finish_request(response):
        response.headers["X-Request-Id"] = getattr(g, "request_id", "")
        duration_ms = (_time.monotonic() - getattr(g, "request_started", _time.monotonic())) * 1000
        try:
            record_http(duration_ms, response.status_code)
        except Exception:  # 指标失败不影响响应
            app.logger.exception("metrics recording failed")
        return response
