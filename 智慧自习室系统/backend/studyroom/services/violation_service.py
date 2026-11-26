from __future__ import annotations

"""违约登记单入口（ST-05/05a/06 的同事务副作用清单，§5.1.1/§5.7）。

调用方必须已处于 immediate_transaction 内。副作用（同一事务）：
插入 violation(U-2) + 预约置 VIOLATED + 删除全部占用 + 计分 + 黑名单判定(ST-13/13a) + outbox。
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
import json

from ..extensions import db
from ..models import (Blacklist, Reservation, SeatOccupancy, Violation,
                      VK_EARLY_LEAVE, VK_NO_CHECKOUT, VK_NO_SHOW, VK_ON_SITE,
                      add_audit, add_outbox)
from ..core.errors import ApiError
from . import credit_service, rule_params
from .timeutil import now

# 学生端默认脱敏模板（Q-20 裁决 C）
MASKED_TEMPLATES = {
    VK_ON_SITE: "现场巡查发现违规使用座位",
    VK_NO_SHOW: "预约开始后未按时签到",
    VK_NO_CHECKOUT: "使用结束后未按时签退",
    VK_EARLY_LEAVE: "提前离开时间超过规定",
}

# 系统判定违约的内置文案（写入 reason 原文列）
SYSTEM_REASONS = {
    VK_NO_SHOW: "预约开始后超过签到窗口仍未签到",
    VK_NO_CHECKOUT: "预约结束后超过宽限期仍未签退",
    VK_EARLY_LEAVE: "提前签退超过早退阈值",
}


@dataclass
class ViolationOutcome:
    violation: Violation
    score: int
    blacklist_triggered: bool
    blacklist_until: datetime | None = None


def record_violation(reservation: Reservation, kind: str, *, operator_id: int | None = None,
                     reason: str | None = None, evidence: str | None = None,
                     on_duplicate: str = "raise", set_status: bool = True,
                     release_all: bool = True,
                     audit_action: str | None = None) -> ViolationOutcome | None:
    """登记一条违约并执行联动。

    on_duplicate="raise"（API-16）：已存在 → 409 DUPLICATE_VIOLATION；
    on_duplicate="skip"（T-1/T-2 扫描/早退）：已存在 → 静默跳过返回 None（幂等，U-2 兜底）。

    set_status/release_all：
    - ST-05/05a/06（NO_SHOW/NO_CHECKOUT/ON_SITE）：True/True —— 预约置 VIOLATED + 删全部占用；
    - ST-04 早退（EARLY_LEAVE）：False/False —— 预约已由签退路径置 COMPLETED，
      占用由签退路径按"后续整点"释放（Q-05），本函数只记违约 + 计分 + 黑名单。
    """
    existing = Violation.query.filter(Violation.reservation_id == reservation.id).first()
    if existing is not None:
        if on_duplicate == "raise":
            raise ApiError("DUPLICATE_VIOLATION", fields={"violationId": existing.id})
        return None

    version, params = rule_params.current()
    written_reason = (reason or SYSTEM_REASONS.get(kind) or MASKED_TEMPLATES.get(kind) or "").strip()
    if evidence:
        written_reason = f"{written_reason}（说明：{evidence}）"

    violation = Violation(
        user_id=reservation.user_id, reservation_id=reservation.id, kind=kind,
        reason=written_reason[:300], operator_id=operator_id,
        rule_version=version, rule_snapshot_json=None,
    )
    violation.rule_snapshot_json = json.dumps(params, ensure_ascii=False, default=str)
    db.session.add(violation)
    db.session.flush()  # 先落库：term_score 的 COUNT 须包含本条（autoflush 语义显式化，防双计）

    if set_status:
        # 状态条件更新（谁先改谁赢，Q-31）：仅当仍是进入本函数时的状态才置 VIOLATED
        expected = reservation.status
        updated = (db.session.query(Reservation)
                   .filter(Reservation.id == reservation.id,
                           Reservation.status == expected)
                   .update({"status": "VIOLATED"}, synchronize_session=False))
        if updated == 0:
            db.session.rollback()
            raise _state_conflict(reservation)
        reservation.status = "VIOLATED"

    if release_all:
        # 释放口径（Q-02 裁决 B）：违约 → 物理删除全部占用
        db.session.query(SeatOccupancy).filter(
            SeatOccupancy.reservation_id == reservation.id).delete(synchronize_session=False)

    # 计分（单一实现：credit_service）；上面已 flush，COUNT 天然含本条
    score = credit_service.term_score(reservation.user_id)

    # 黑名单判定（ST-13 / ST-13a），写前先关闭已过期行（U-8）
    credit_service.close_expired(reservation.user_id)
    triggered = False
    until = None
    active = credit_service.active_blacklist(reservation.user_id)
    if active is not None:
        # 期内再违约：n 不变，until = MAX(until, now + days(n))，不新建第二条（Q-09 裁决 A）
        n = credit_service.entries_in_term(reservation.user_id)
        days = credit_service.blacklist_days(n, params)
        candidate = now() + timedelta(days=days)
        if candidate > active.blacklist_until:
            active.blacklist_until = candidate
        until = active.blacklist_until
    elif score >= int(params["violationScoreThreshold"]):
        n = credit_service.entries_in_term(reservation.user_id) + 1  # 进入前 +1（Q-08）
        days = credit_service.blacklist_days(n, params)
        until = now() + timedelta(days=days)
        db.session.add(Blacklist(user_id=reservation.user_id, source="AUTO",
                                 triggered_by_violation_id=None, blacklist_until=until,
                                 rule_version=version))
        triggered = True

    db.session.flush()  # 拿 violation.id

    add_outbox("VIOLATION_CREATED", reservation.user_id, {
        "violationId": violation.id, "kind": kind, "reservationId": reservation.id,
    })
    if audit_action:
        add_audit(audit_action, target_type="reservation", target_id=reservation.id,
                  reason=written_reason, rule_version=version, result_code="VIOLATED")
    elif operator_id is None:
        add_audit("VIOLATION_AUTO", target_type="reservation", target_id=reservation.id,
                  rule_version=version, result_code="VIOLATED")

    return ViolationOutcome(violation=violation, score=score,
                            blacklist_triggered=triggered, blacklist_until=until)


def _state_conflict(reservation: Reservation) -> ApiError:
    db.session.refresh(reservation)
    return ApiError("RESERVATION_STATE_CONFLICT",
                    fields={"currentStatus": reservation.status})
