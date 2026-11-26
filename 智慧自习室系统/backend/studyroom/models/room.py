from __future__ import annotations

from ..extensions import db
from .base import TimestampMixin


class Room(db.Model, TimestampMixin):
    __tablename__ = "room"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False, unique=True)
    location = db.Column(db.String(120), nullable=False)
    # 必填无默认（Q-21 裁决 A）；closeHour=24 表示"到当天结束"，时刻表达为次日 T00:00（§3.1）
    open_hour = db.Column(db.Integer, nullable=False)
    close_hour = db.Column(db.Integer, nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    # 乐观锁（API-13b）
    version = db.Column(db.Integer, nullable=False, default=1)

    seats = db.relationship("Seat", backref="room", cascade="all, delete-orphan")
