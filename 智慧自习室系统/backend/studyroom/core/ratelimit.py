from __future__ import annotations

import time
from collections import deque
from threading import Lock

from flask import request

from .errors import ApiError

_lock = Lock()
_hits: dict[str, deque] = {}
_WINDOW_SECONDS = 60.0


def _allow(key: str, limit: int) -> bool:
    now_ts = time.monotonic()
    with _lock:
        q = _hits.setdefault(key, deque())
        while q and q[0] <= now_ts - _WINDOW_SECONDS:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now_ts)
        return True


def _actor_id_from_token() -> int | None:
    """尽力从令牌取 sub（不查库：鉴权仍由装饰器负责）。"""
    import jwt
    from flask import current_app
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    try:
        payload = jwt.decode(header[7:].strip(), current_app.config["SECRET_KEY"],
                             algorithms=["HS256"])
        return int(payload["sub"])
    except Exception:
        return None


def install(app) -> None:
    @app.before_request
    def _rate_limit():
        # /health 与静态资源不限
        if request.path == "/health" or request.endpoint == "static":
            return None
        if not request.path.startswith("/api/"):
            return None

        is_login = request.path == "/api/v1/auth/login" and request.method == "POST"
        if is_login:
            # 登录限流：单账号 5/分 + 单 IP 30/分（§3.6 初值）
            data = request.get_json(silent=True) or {}
            username = str(data.get("username") or "")[:64]
            if username and not _allow(f"login:acct:{username}",
                                       app.config["RATE_LIMIT_LOGIN_PER_ACCOUNT"]):
                raise ApiError("RATE_LIMITED")
            ip = request.remote_addr or "unknown"
            if not _allow(f"login:ip:{ip}", app.config["RATE_LIMIT_LOGIN_PER_IP"]):
                raise ApiError("RATE_LIMITED")
            return None

        actor_id = _actor_id_from_token()
        if actor_id is None:
            return None  # 未认证请求由鉴权装饰器拒绝
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            if not _allow(f"write:u:{actor_id}", app.config["RATE_LIMIT_WRITE_PER_ACCOUNT"]):
                raise ApiError("RATE_LIMITED")
        else:
            if not _allow(f"query:u:{actor_id}", app.config["RATE_LIMIT_QUERY_PER_ACCOUNT"]):
                raise ApiError("RATE_LIMITED")
        return None
