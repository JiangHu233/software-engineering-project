from __future__ import annotations

"""违约计分与黑名单判定（§5.5）。

设计红线：termScore / blacklistDays 只有这一处实现；
API-03/06/12/16 与扫描任务全部调用本模块，禁止各自计数。
"""
from datetime import datetime, timedelta

from ..extensions import db
from ..models import Blacklist, Violation
from . import rule_params
from .timeutil import now, parse_date


def term_start(params: dict | None = None) -> datetime:
    params = params or rule_params.current()[1]
    return datetime.combine(parse_date(str(params["semesterStart"]), "semesterStart"),
                            datetime.min.time())


def term_score(user_id: int) -> int:
    """COUNT(violation WHERE user_id=u AND created_at>=termStart AND waived_at IS NULL)。"""
    _, params = rule_params.current()
    return Violation.query.filter(
        Violation.user_id == user_id,
        Violation.created_at >= term_start(params),
        Violation.waived_at.is_(None),
    ).count()


def entries_in_term(user_id: int) -> int:
    """n = COUNT(blacklist WHERE user_id=u AND created_at>=termStart AND revoked_at IS NULL)。"""
    _, params = rule_params.current()
    return Blacklist.query.filter(
        Blacklist.user_id == user_id,
        Blacklist.created_at >= term_start(params),
        Blacklist.revoked_at.is_(None),
    ).count()


def blacklist_days(n: int, params: dict | None = None) -> int:
    params = params or rule_params.current()[1]
    return min(int(params["blacklistDaysPerEntry"]) * n, int(params["blacklistMaxDays"]))


def active_blacklist(user_id: int) -> Blacklist | None:
    """读路径一律按 blacklist_until > now() 实时判定（ST-14a 兜底，不读状态字段）。"""
    bl = (Blacklist.query
          .filter(Blacklist.user_id == user_id, Blacklist.released_at.is_(None),
                  Blacklist.revoked_at.is_(None))
          .order_by(Blacklist.id.desc())
          .first())
    if bl is not None and bl.blacklist_until > now():
        return bl
    return None


def is_blacklisted(user_id: int) -> bool:
    return active_blacklist(user_id) is not None


def close_expired(user_id: int, at: datetime | None = None) -> None:
    """U-8 写前关闭（§5.3）：把已到期但未关闭的记录关掉，避免部分唯一索引误挡新建。
    裁定#6：条件扩展含 revoked（为 S2 申诉撤销兜底）。须在写事务内调用。"""
    at = at or now()
    bl = (Blacklist.query
          .filter(Blacklist.user_id == user_id, Blacklist.released_at.is_(None))
          .order_by(Blacklist.id.desc())
          .first())
    if bl is not None and (bl.blacklist_until <= at or bl.revoked_at is not None):
        bl.released_at = bl.blacklist_until
        db.session.flush()
