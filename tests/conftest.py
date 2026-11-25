import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from app import create_app, db  # noqa: E402


@pytest.fixture
def app(tmp_path):
    test_app = create_app({
        "TESTING": True,
        "SECRET_KEY": "test-secret",
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'test.db'}",
        "WTF_CSRF_ENABLED": False,
    })
    yield test_app
    with test_app.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def login(client, username="student", password="student123"):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=True)
