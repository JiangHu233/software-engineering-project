from __future__ import annotations

import json
from datetime import datetime

from ..extensions import db
from .base import TimestampMixin


class RuleParameter(db.Model, TimestampMixin):
    __tablename__ = "rule_parameter"

    id = db.Column(db.Integer, primary_key=True)
    scope = db.Column(db.String(10), nullable=False, default="GLOBAL")
    version = db.Column(db.Integer, nullable=False)
    params_json = db.Column(db.Text, nullable=False)
    effective_from = db.Column(db.DateTime, nullable=False, default=datetime.now)
    # NULL = 当前生效
    effective_to = db.Column(db.DateTime)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    reason = db.Column(db.String(500), nullable=False)

    # U-7：版本号唯一，防并发写同号
    __table_args__ = (
        db.UniqueConstraint("scope", "version", name="u7_rule_param_version"),
    )

    @property
    def params(self) -> dict:
        return json.loads(self.params_json)

    @params.setter
    def params(self, value: dict) -> None:
        self.params_json = json.dumps(value, ensure_ascii=False)
