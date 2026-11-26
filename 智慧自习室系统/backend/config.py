"""后端配置：环境差异化 + 可调运行参数（NFR-12：业务阈值不落在这里，一律进 rule_parameter 表）。"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


class BaseConfig:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-change-before-sharing-0123456789")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{BASE_DIR / 'instance' / 'studyroom.db'}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # busy_timeout（毫秒）：SQLite 写锁等待上限，超时映射为 409 SLOT_TAKEN/LOCK_TIMEOUT（Q-29）
    DB_BUSY_TIMEOUT_MS = int(os.environ.get("DB_BUSY_TIMEOUT_MS", "5000"))
    ENABLE_SCHEDULER = os.environ.get("ENABLE_SCHEDULER", "1") == "1"
    # 限流初值（§3.6，待运维确认；单实例内存实现）
    RATE_LIMIT_LOGIN_PER_ACCOUNT = 5
    RATE_LIMIT_LOGIN_PER_IP = 30
    RATE_LIMIT_WRITE_PER_ACCOUNT = 30
    RATE_LIMIT_QUERY_PER_ACCOUNT = 120
    # 幂等记录保留时长（小时，§3.7 可配置）
    IDEMPOTENCY_TTL_HOURS = 24
    ADMIN_INITIAL_PASSWORD = os.environ.get("ADMIN_INITIAL_PASSWORD", "admin123")


class DevConfig(BaseConfig):
    DEBUG = True


class TestConfig(BaseConfig):
    TESTING = True
    ENABLE_SCHEDULER = False
    RATE_LIMIT_LOGIN_PER_ACCOUNT = 1000
    RATE_LIMIT_LOGIN_PER_IP = 1000
    RATE_LIMIT_WRITE_PER_ACCOUNT = 1000
    RATE_LIMIT_QUERY_PER_ACCOUNT = 1000


class ProdConfig(BaseConfig):
    SECRET_KEY = os.environ.get("SECRET_KEY") or BaseConfig.SECRET_KEY


def load_config(name: str | None = None) -> type[BaseConfig]:
    name = (name or os.environ.get("FLASK_ENV") or "dev").lower()
    return {"dev": DevConfig, "test": TestConfig, "prod": ProdConfig}[name]
