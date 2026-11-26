from __future__ import annotations

"""T-4 学期初提醒（每天）：semesterStart 疑似未调整时只写审计告警，不改参数（Q-11 裁决 A）。"""
from datetime import date

from ..extensions import db
from ..models import AuditLog, add_audit
from ..core.db_txn import immediate_transaction
from ..services import rule_params
from ..services.timeutil import parse_date


def semester_reminder() -> dict:
    _, params = rule_params.current()
    semester_start = parse_date(str(params["semesterStart"]), "semesterStart")
    today = date.today()
    alerted = False
    # 与 semesterStart 同月且距今超过 180 天 → 疑似忘调参数
    if semester_start.month == today.month and (today - semester_start).days > 180:
        dedup_key = f"{semester_start.isoformat()}:{today.isoformat()}"
        exists = AuditLog.query.filter(
            AuditLog.action == "SEMESTER_START_STALE_ALERT",
            AuditLog.reason == f"len={len(dedup_key)}:{dedup_key[:20]}").first()
        if not exists:
            with immediate_transaction():
                add_audit("SEMESTER_START_STALE_ALERT", reason=dedup_key,
                          result_code="ALERT")
            alerted = True
    return {"alerted": alerted, "semesterStart": semester_start.isoformat()}
