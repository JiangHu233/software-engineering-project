from __future__ import annotations

import json

from ..extensions import db
from .base import TimestampMixin

SEAT_AVAILABLE = "AVAILABLE"
SEAT_MAINTENANCE = "MAINTENANCE"
SEAT_DISABLED = "DISABLED"


class Seat(db.Model, TimestampMixin):
    __tablename__ = "seat"

    id = db.Column(db.Integer, primary_key=True)
    room_id = db.Column(db.Integer, db.ForeignKey("room.id"), nullable=False, index=True)
    label = db.Column(db.String(20), nullable=False)
    # 取值必须来自 zone_dictionary（Q-06 裁决 B），存字典 label
    zone = db.Column(db.String(40), nullable=False)
    # JSON 文本，存 string[]；序列化层负责 ↔ list 转换
    features_json = db.Column(db.String(400), nullable=False, default="[]")
    # 唯一权威状态字段（§5.1.2）；"某时段是否空闲"不落字段，由 C-7 实时计算
    state = db.Column(db.String(12), nullable=False, default=SEAT_AVAILABLE)

    __table_args__ = (
        db.UniqueConstraint("room_id", "label", name="u3_seat_room_label"),
    )

    @property
    def features(self) -> list[str]:
        return json.loads(self.features_json or "[]")

    @features.setter
    def features(self, value: list[str]) -> None:
        self.features_json = json.dumps(list(value), ensure_ascii=False)


class ZoneDictionary(db.Model):
    __tablename__ = "zone_dictionary"

    code = db.Column(db.String(20), primary_key=True)
    label = db.Column(db.String(40), nullable=False, unique=True)
    active = db.Column(db.Boolean, nullable=False, default=True)
