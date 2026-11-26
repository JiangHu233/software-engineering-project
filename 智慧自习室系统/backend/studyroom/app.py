from __future__ import annotations

"""应用工厂：配置 → 扩展 → 横切设施 → 蓝图 → 调度器。

对比原型 src/app.py 的关键修正（§5.9 D-1~D-13）：
- 无 import 副作用：不在此建库/写种子（init-db CLI 负责）；
- 无 before_request 业务扫描（D-5）：违约判定移交 APScheduler T-1/T-2；
- REST + JWT + 统一错误码（D-1/D-2）；幂等键（D-4）；参数可配置（D-10）。
"""
from pathlib import Path

from flask import Flask, jsonify

from config import load_config


def create_app(config_name: str | None = None, test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    config_cls = load_config(config_name)
    app.config.from_object(config_cls)
    if test_config:
        app.config.update(test_config)

    instance_dir = Path(app.instance_path if app.instance_path else Path("instance"))
    db_uri = app.config["SQLALCHEMY_DATABASE_URI"]
    if db_uri.startswith("sqlite:///"):
        db_path = Path(db_uri[len("sqlite:///"):])
        if not db_path.is_absolute():
            db_path = Path(app.root_path) / db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)

    from .extensions import db
    db.init_app(app)
    from .core.db_txn import install_engine_pragmas
    install_engine_pragmas(app)

    from .core import handlers, request_context, ratelimit
    handlers.install(app)
    request_context.install(app)
    ratelimit.install(app)

    from .api import api_bp
    from .api.v1 import (auth, seats, reservations, me, admin_rooms, admin_seats,
                         admin_reservations, admin_users, rule_params, ops)
    app.register_blueprint(api_bp)

    from . import cli
    cli.register(app)

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    from .tasks.scheduler import init_scheduler
    init_scheduler(app)

    return app
