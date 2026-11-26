from __future__ import annotations

from datetime import datetime

from sqlalchemy import Index, text

from ..extensions import db
from .base import TimestampMixin

VK_NO_SHOW = "NO_SHOW"
VK_NO_CHECKOUT = "NO_CHECKOUT"
VK_EARLY_LEAVE = "EARLY_LEAVE"
VK_ON_SITE = "ON_SITE"


class Violation(db.Model, TimestampMixin):
    __tablename__ = "violation"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    # U-2：一条预约至多一条违约（EX-11/AV-03④/AV-06④）
    reservation_id = db.Column(db.Integer, db.ForeignKey("reservation.id"), nullable=False, unique=True)
    kind = db.Column(db.String(12), nullable=False)
    reason = db.Column(db.String(300), nullable=False)
    operator_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    # 判定时刻的参数版本（§5.5 规则版本）
    rule_version = db.Column(db.Integer)
    rule_snapshot_json = db.Column(db.Text)
    # 申诉豁免预留（ADR-3/F-10：只标记不删除），S1 不使用
    waived_at = db.Column(db.DateTime)
    waived_reason = db.Column(db.String(200))

    user = db.relationship("User", foreign_keys=[user_id], backref="violations")
    operator = db.relationship("User", foreign_keys=[operator_id])

    __table_args__ = (
        db.Index("ix_violation_user_created", "user_id", "created_at"),
    )

    @property
    def rule_snapshot(self) -> dict:
        import json
        return json.loads(self.rule_snapshot_json or "{}")


class Blacklist(db.Model, TimestampMixin):
    __tablename__ = "blacklist"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    source = db.Column(db.String(8), nullable=False, default="AUTO")  # S1 只用 AUTO
    triggered_by_violation_id = db.Column(db.Integer, db.ForeignKey("violation.id"))
    blacklist_until = db.Column(db.DateTime, nullable=False)
    released_at = db.Column(db.DateTime)
    # 申诉撤销预留（Q-10/R-5），S1 不使用
    revoked_at = db.Column(db.DateTime)
    revoked_reason = db.Column(db.String(200))
    rule_version = db.Column(db.Integer)

    # U-8 部分唯一索引：同一用户至多一条"未关闭"记录；写前先关闭已过期行（§5.3）
    __table_args__ = (
        Index(
            "u8_blacklist_active",
            "user_id",
            unique=True,
            sqlite_where=text("released_at IS NULL"),
        ),
    )
