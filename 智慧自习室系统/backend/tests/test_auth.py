"""认证与会话：API-01/02/03 + 防枚举 + 吊销 + 停用即时生效。"""
import uuid


def test_login_success(client, stu_headers):
    r = client.get("/api/v1/auth/me", headers=stu_headers)
    assert r.status_code == 200
    data = r.get_json()["data"]
    assert data["user"]["role"] == "STU"
    assert data["blacklist"]["active"] is False
    assert "passwordHash" not in str(data)


def test_login_wrong_password_no_enumeration(client):
    r1 = client.post("/api/v1/auth/login", json={"username": "stu1", "password": "wrong-pass"})
    r2 = client.post("/api/v1/auth/login", json={"username": "no-such-user", "password": "wrong-pass"})
    assert r1.status_code == r2.status_code == 401
    assert r1.get_json()["error"]["code"] == r2.get_json()["error"]["code"] == "UNAUTHENTICATED"


def test_login_password_length(client):
    r = client.post("/api/v1/auth/login", json={"username": "stu1", "password": "short"})
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "VALIDATION_FAILED"


def test_logout_revokes_token(client, stu_headers):
    r = client.post("/api/v1/auth/logout", headers=stu_headers)
    assert r.status_code == 200
    r = client.get("/api/v1/auth/me", headers=stu_headers)
    assert r.status_code == 401


def test_no_token_401(client):
    r = client.get("/api/v1/auth/me")
    assert r.status_code == 401
    assert r.get_json()["error"]["code"] == "UNAUTHENTICATED"


def test_error_envelope_shape(client):
    r = client.get("/api/v1/auth/me")
    err = r.get_json()["error"]
    for key in ("code", "message", "requestId"):
        assert key in err
    assert "data" not in r.get_json()


def test_disabled_account_rejected_immediately(client, adm_headers, stu_headers):
    from studyroom.models import User
    from studyroom.extensions import db as _db
    with client.application.app_context():
        uid = User.query.filter_by(username="stu1").first().id
    r = client.patch(f"/api/v1/admin/users/{uid}", headers={**adm_headers, "Idempotency-Key": str(uuid.uuid4())},
                     json={"accountStatus": "DISABLED", "reason": "测试"})
    assert r.status_code == 200
    r = client.get("/api/v1/auth/me", headers=stu_headers)
    assert r.status_code == 401
    assert r.get_json()["error"]["code"] == "ACCOUNT_DISABLED"
    # 恢复
    client.patch(f"/api/v1/admin/users/{uid}", headers={**adm_headers, "Idempotency-Key": str(uuid.uuid4())},
                 json={"accountStatus": "ACTIVE", "reason": "恢复"})
