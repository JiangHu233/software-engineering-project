from __future__ import annotations

"""T-3 黑名单期满（每小时）：条件写 released_at + outbox；顺带清理过期 token_revocation。

事件类型 BLACKLIST_RELEASED 为 T-3 专用（§5.6 只要求"写 outbox"，未列入 Q-13 的五类
业务事件，此处扩展一枚举值，S2 投递器同样消费）。
"""
from ..extensions import db
from ..models import Blacklist, TokenRevocation, add_outbox
from ..core.db_txn import immediate_transaction
from ..services.timeutil import now as now_fn


def expire_blacklists() -> dict:
    t_now = now_fn()
    released = 0
    with immediate_transaction():
        expired = Blacklist.query.filter(
            Blacklist.released_at.is_(None), Blacklist.blacklist_until <= t_now).all()
        for bl in expired:
            bl.released_at = bl.blacklist_until
            add_outbox("BLACKLIST_RELEASED", bl.user_id, {"blacklistId": bl.id})
        released = len(expired)
        # 吊销名单只保留到 exp 之后（§3.2），顺带清理
        db.session.query(TokenRevocation).filter(
            TokenRevocation.token_exp < t_now).delete(synchronize_session=False)
    return {"released": released}
