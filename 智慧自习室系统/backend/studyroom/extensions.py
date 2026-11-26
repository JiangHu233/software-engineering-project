from __future__ import annotations

from sqlalchemy import MetaData
from flask_sqlalchemy import SQLAlchemy

# 统一命名约定：保证唯一约束/索引名字稳定，供 IntegrityError 按名映射（§3.4/handlers.py）
naming_convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

db = SQLAlchemy(metadata=MetaData(naming_convention=naming_convention))
