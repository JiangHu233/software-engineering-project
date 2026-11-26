"""认证与会话：API-01/02/03 + 防枚举 + 吊销 + 停用即时生效 + S2-01 黑名单作用域/越权扫描。"""
import uuid

from tests.conftest import day


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


# ================================================================ S2-01 黑名单作用域


def _blacklist_stu1(client):
    from studyroom.models import Blacklist, User
    from studyroom.extensions import db as _db
    from datetime import datetime as dt, timedelta
    with client.application.app_context():
        uid = User.query.filter_by(username="stu1").first().id
        _db.session.add(Blacklist(user_id=uid, source="AUTO",
                                  blacklist_until=dt.now() + timedelta(days=3)))
        _db.session.commit()


def test_blacklisted_user_can_login_and_query(client, stu_headers, room_and_seat):
    """TC-E2E-03：黑名单只拦创建预约；登录/查我的预约/查座位仍可用。"""
    room_id, _ = room_and_seat
    _blacklist_stu1(client)
    r = client.get("/api/v1/auth/me", headers=stu_headers)
    assert r.status_code == 200
    assert r.get_json()["data"]["blacklist"]["active"] is True
    r = client.get("/api/v1/me/reservations", headers=stu_headers)
    assert r.status_code == 200
    r = client.get(f"/api/v1/rooms/{room_id}/seats?date={day(1)}", headers=stu_headers)
    assert r.status_code == 200


def test_blacklisted_user_create_rejected_with_release_date(client, stu_headers, room_and_seat):
    """TC-E2E-03：黑名单创建预约被拒且含解除日期。"""
    _, seat_id = room_and_seat
    _blacklist_stu1(client)
    r = client.post("/api/v1/reservations", headers={**stu_headers, "Idempotency-Key": str(uuid.uuid4())},
                    json={"seatId": seat_id, "startsAt": f"{day(5)}T10:00:00",
                          "endsAt": f"{day(5)}T11:00:00"})
    assert r.status_code == 422
    err = r.get_json()["error"]
    assert err["code"] == "BLACKLISTED"
    assert "blacklistUntil" in err["fields"]


# ================================================================ 越权扫描 + 审计


ADMIN_ROUTES = [
    ("GET", "/api/v1/admin/rooms/9999/board"),
    ("GET", "/api/v1/admin/reservations/in-use"),
    ("GET", "/api/v1/admin/reservations"),
    ("GET", "/api/v1/admin/users/9999"),
    ("PATCH", "/api/v1/admin/users/9999"),
    ("PATCH", "/api/v1/admin/rooms/9999"),
    ("POST", "/api/v1/admin/rooms"),
    ("POST", "/api/v1/admin/rooms/9999/seats"),
    ("PATCH", "/api/v1/admin/seats/9999"),
    ("POST", "/api/v1/admin/reservations/9999/violations"),
    ("GET", "/api/v1/admin/rule-parameters"),
    ("PUT", "/api/v1/admin/rule-parameters"),
]


def _scan_admin_as_student(client, stu_headers) -> int:
    calls = 0
    for method, path in ADMIN_ROUTES:
        kw = {"headers": {**stu_headers, "Idempotency-Key": str(uuid.uuid4())}}
        if method in ("POST", "PATCH", "PUT"):
            kw["json"] = {}
        r = getattr(client, method.lower())(path, **kw)
        assert r.status_code == 403, f"{method} {path}: {r.status_code} {r.get_json()}"
        assert r.get_json()["error"]["code"] == "FORBIDDEN_ROLE", f"{method} {path}"
        calls += 1
    return calls


def test_student_scan_all_admin_endpoints_403(client, stu_headers):
    """学生令牌遍历全部管理接口，逐个 100% 被拒（403 FORBIDDEN_ROLE）。"""
    assert _scan_admin_as_student(client, stu_headers) == len(ADMIN_ROUTES)


def test_student_scan_writes_audit_each_call(client, stu_headers):
    """越权扫描：审计条数 == 越权调用次数（每一步留痕）。"""
    from studyroom.models import AuditLog
    from studyroom.extensions import db as _db
    calls = _scan_admin_as_student(client, stu_headers)
    with client.application.app_context():
        n = AuditLog.query.filter_by(action="FORBIDDEN_ROLE").count()
    assert n == calls
