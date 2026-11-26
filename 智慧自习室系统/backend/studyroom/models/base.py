from __future__ import annotations

from datetime import datetime

from ..extensions import db


class TimestampMixin:
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
