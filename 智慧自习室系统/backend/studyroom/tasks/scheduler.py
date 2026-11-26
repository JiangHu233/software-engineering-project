from __future__ import annotations

"""APScheduler 集成（ADR-7）：单实例；TESTING/ENABLE_SCHEDULER=False 不启动；
启动时同步补偿执行一次全部任务（条件查询天然幂等，停机 3 天一次补齐）。"""
import os

from flask import Flask


def init_scheduler(app: Flask) -> None:
    if app.config.get("TESTING"):
        return
    if not app.config.get("ENABLE_SCHEDULER", True):
        return
    # 防 debug reloader 双开：仅 reloader 主进程注册
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
        pass
    else:
        return

    from apscheduler.schedulers.background import BackgroundScheduler
    from .blacklist_expire import expire_blacklists
    from .scan_timeouts import scan_no_checkout, scan_no_show
    from .semester_reminder import semester_reminder

    scheduler = BackgroundScheduler(daemon=True)
    common = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 60}

    def job(fn):
        def wrapped():
            with app.app_context():
                fn()
        return wrapped

    scheduler.add_job(job(scan_no_show), "interval", minutes=1, id="T-1", **common)
    scheduler.add_job(job(scan_no_checkout), "interval", minutes=1, id="T-2", **common)
    scheduler.add_job(job(expire_blacklists), "interval", hours=1, id="T-3", **common)
    scheduler.add_job(job(semester_reminder), "cron", hour=8, minute=30, id="T-4", **common)
    scheduler.start()
    app.extensions["scheduler"] = scheduler

    # 启动补偿：立即同步跑一次（ADR-7；失败只告警不阻断启动）
    with app.app_context():
        for fn in (scan_no_show, scan_no_checkout, expire_blacklists):
            try:
                fn()
            except Exception:
                app.logger.exception("startup compensation failed: %s", fn.__name__)
