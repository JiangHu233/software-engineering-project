from __future__ import annotations

from datetime import datetime

from werkzeug.security import check_password_hash, generate_password_hash

from ..extensions import db
from .base import TimestampMixin

ROLE_STU = "STU"
ROLE_ADM = "ADM"
ROLE_OPS = "OPS"


class User(db.Model, TimestampMixin):
    __tablename__ = "user"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(40), nullable=False, unique=True)
    display_name = db.Column(db.String(60), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(8), nullable=False, default=ROLE_STU)
    account_status = db.Column(db.String(10), nullable=False, default="ACTIVE")
    last_login_at = db.Column(db.DateTime)
    # 乐观锁（API-18b 并发停用）
    version = db.Column(db.Integer, nullable=False, default=1)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    @property
    def is_active_account(self) -> bool:
        return self.account_status == "ACTIVE"
