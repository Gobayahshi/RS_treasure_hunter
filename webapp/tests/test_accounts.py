"""계정 통합 테스트 - SKT 총괄/직원, 대리점 직원(사원 고유ID), 임시 계정 제거."""

import uuid
from datetime import datetime

from test_inventory import build_inventory_xlsx


def admin_auth(token):
    return {"X-Admin-Token": token}


def create_staff(client, admin_token, username=None, password="staff1234"):
    """SKT 직원 계정을 만들고 첫 로그인 후 초기 비밀번호를 바꾼다.

    발급받은 초기 비밀번호는 바꾸기 전까지 다른 API 가 막히므로(PASSWORD_CHANGE_REQUIRED),
    실제 사용 흐름대로 한 번 바꿔 둔다.
    """
    username = username or f"skt{uuid.uuid4().hex[:6]}"
    res = client.post(
        "/api/admin/accounts",
        json={"username": username, "password": password},
        headers=admin_auth(admin_token),
    )
    assert res.status_code == 201, res.get_json()
    token = client.post(
        "/api/admin/login", json={"username": username, "password": password}
    ).get_json()["token"]
    changed = client.post(
        "/api/admin/change-password",
        json={"current_password": password, "new_password": f"{password}-set"},
        headers=admin_auth(token),
    )
    assert changed.status_code == 200, changed.get_json()
    return res.get_json(), token


# ---------------------------------------------------------------------------
# 임시 대리점 계정 제거
# ---------------------------------------------------------------------------


def test_legacy_dealer_accounts_are_removed_on_boot(server):
    from werkzeug.security import generate_password_hash

    from db import db_session, init_db

    now = datetime.utcnow().isoformat()
    with db_session() as conn:
        conn.execute(
            "INSERT INTO admins (id, username, password_hash, created_at, dealer_id, role)"
            " VALUES ('dealer-yuwon', 'yuwon', ?, ?, 'some-dealer', 'dealer')",
            (generate_password_hash("yuwon"), now),
        )
        conn.execute(
            "INSERT INTO admin_sessions (token, admin_id, created_at) VALUES ('legacy-token', 'dealer-yuwon', ?)",
            (now,),
        )
    init_db()
    with db_session() as conn:
        assert conn.execute("SELECT 1 FROM admins WHERE username = 'yuwon'").fetchone() is None
        assert conn.execute("SELECT 1 FROM admin_sessions WHERE token = 'legacy-token'").fetchone() is None
        # SKT 총괄은 남는다
        assert conn.execute("SELECT 1 FROM admins WHERE username = 'admin'").fetchone() is not None


def test_legacy_dealer_account_cannot_log_in(client):
    res = client.post("/api/inventory/login", json={"username": "yuwon", "password": "yuwon"})
    assert res.status_code == 401


# ---------------------------------------------------------------------------
# 대리점 직원 = 영업사원 고유ID
# ---------------------------------------------------------------------------


def test_rep_logs_into_inventory_with_employee_code(client, fixtures):
    res = client.post(
        "/api/inventory/login",
        json={"username": fixtures["employee_code"], "password": fixtures["password"]},
    )
    assert res.status_code == 200
    body = res.get_json()
    assert body["role"] == "dealer"
    assert body["dealer_role"] == "staff"
    assert body["dealer_id"] == fixtures["dealer_id"]
    assert body["can_see_all"] is False
    assert body["can_upload"] is True

    me = client.get("/api/inventory/me", headers=admin_auth(body["token"])).get_json()
    assert me["dealer_id"] == fixtures["dealer_id"]
    assert me["name"] == "홍길동"


def test_rep_session_works_in_both_apps(client, fixtures, rep_token):
    """보물찾기에서 받은 토큰으로 재고 화면도 쓸 수 있다 (같은 사람, 같은 ID)."""
    assert client.get("/api/inventory/me", headers={"X-Rep-Token": rep_token}).status_code == 200


def test_rep_without_dealer_cannot_use_inventory(client, server):
    from db import db_session

    code = uuid.uuid4().hex[:7]
    with db_session() as conn:
        conn.execute(
            "INSERT INTO reps (id, dealer_id, name, employee_code, password_hash, created_at)"
            " VALUES (?, NULL, '무소속', ?, ?, ?)",
            (uuid.uuid4().hex, code, server.hash_password("pw1234"), datetime.utcnow().isoformat()),
        )
    res = client.post("/api/inventory/login", json={"username": code, "password": "pw1234"})
    assert res.status_code == 403
    assert res.get_json()["error"] == "NO_DEALER"

    # 보물찾기 토큰으로 우회해도 재고 화면은 막힌다
    token = client.post(
        "/api/auth/login", json={"employee_code": code, "password": "pw1234"}
    ).get_json()["token"]
    assert client.get("/api/inventory/me", headers={"X-Rep-Token": token}).status_code == 401


def test_rep_wrong_password_on_inventory(client, fixtures):
    res = client.post(
        "/api/inventory/login",
        json={"username": fixtures["employee_code"], "password": "wrong"},
    )
    assert res.status_code == 401


def test_any_dealer_staff_can_upload_inventory(client, fixtures):
    token = client.post(
        "/api/inventory/login",
        json={"username": fixtures["employee_code"], "password": fixtures["password"]},
    ).get_json()["token"]
    data = build_inventory_xlsx(
        [[fixtures["store_code"], "테스트판매점", "갤럭시", "SM-F971", "100", 3, "S1", "테스트대리점"]]
    )
    res = client.post(
        "/api/inventory/excel",
        data={"file": (__import__("io").BytesIO(data), "재고.xlsx"), "job_id": uuid.uuid4().hex},
        headers=admin_auth(token),
        content_type="multipart/form-data",
    )
    assert res.status_code == 202, res.get_json()


def test_rep_token_cannot_reach_admin_console(client, rep_token):
    assert client.get("/api/reps", headers=admin_auth(rep_token)).status_code == 401
    assert client.get("/api/admin/me", headers=admin_auth(rep_token)).status_code == 401


# ---------------------------------------------------------------------------
# SKT 직원 (조회 전용)
# ---------------------------------------------------------------------------


def test_super_creates_staff_account(client, admin_token):
    account, token = create_staff(client, admin_token)
    assert account["role"] == "staff"
    me = client.get("/api/admin/me", headers=admin_auth(token)).get_json()
    assert me["role"] == "staff"
    assert me["can_edit"] is False

    listed = client.get("/api/admin/accounts", headers=admin_auth(admin_token)).get_json()
    assert account["id"] in [a["id"] for a in listed]


def test_staff_can_read_but_not_write(client, admin_token, fixtures):
    _, token = create_staff(client, admin_token)
    H = admin_auth(token)

    for path in ("/api/reps", "/api/dealers", "/api/stores", "/api/points", "/api/admin/visit-sessions",
                 "/api/admin/settings", "/api/admin/treasures"):
        assert client.get(path, headers=H).status_code == 200, path

    writes = [
        ("post", "/api/admin/settings", {"points_normal": 1, "points_rare": 2}),
        ("post", "/api/treasures/spawn", {}),
        ("post", "/api/reps", {"name": "x", "employee_code": "x"}),
        ("post", "/api/admin/accounts", {"username": "x", "password": "xxxx"}),
        ("get", "/api/admin/accounts", None),
    ]
    for method, path, body in writes:
        res = getattr(client, method)(path, json=body, headers=H) if body is not None else getattr(client, method)(path, headers=H)
        assert res.status_code == 403, (path, res.status_code)


def test_staff_can_change_dealer_role(client, admin_token, fixtures):
    _, token = create_staff(client, admin_token)
    res = client.patch(
        f"/api/reps/{fixtures['rep_id']}/dealer-role",
        json={"dealer_role": "manager"},
        headers=admin_auth(token),
    )
    assert res.status_code == 200
    assert res.get_json()["dealer_role"] == "manager"

    # 재고 화면 로그인에도 반영된다
    body = client.post(
        "/api/inventory/login",
        json={"username": fixtures["employee_code"], "password": fixtures["password"]},
    ).get_json()
    assert body["dealer_role"] == "manager"

    bad = client.patch(
        f"/api/reps/{fixtures['rep_id']}/dealer-role",
        json={"dealer_role": "super"},
        headers=admin_auth(token),
    )
    assert bad.status_code == 400


def test_rep_cannot_change_dealer_role(client, fixtures, rep_token):
    res = client.patch(
        f"/api/reps/{fixtures['rep_id']}/dealer-role",
        json={"dealer_role": "manager"},
        headers=admin_auth(rep_token),
    )
    assert res.status_code == 401


def test_staff_views_all_dealers_in_inventory_but_cannot_upload(client, admin_token):
    username = f"skt{uuid.uuid4().hex[:6]}"
    create_staff(client, admin_token, username=username)
    body = client.post(
        "/api/inventory/login", json={"username": username, "password": "staff1234-set"}
    ).get_json()
    assert body["can_see_all"] is True
    assert body["can_upload"] is False

    res = client.post(
        "/api/inventory/excel",
        data={"file": (__import__("io").BytesIO(b"x"), "재고.xlsx")},
        headers=admin_auth(body["token"]),
        content_type="multipart/form-data",
    )
    assert res.status_code == 403
    assert res.get_json()["error"] == "VIEW_ONLY"


def test_staff_username_cannot_collide_with_employee_code(client, admin_token, fixtures):
    res = client.post(
        "/api/admin/accounts",
        json={"username": fixtures["employee_code"], "password": "staff1234"},
        headers=admin_auth(admin_token),
    )
    assert res.status_code == 409


def test_deleting_staff_account_ends_session(client, admin_token):
    account, token = create_staff(client, admin_token)
    assert client.get("/api/admin/me", headers=admin_auth(token)).status_code == 200
    res = client.delete(f"/api/admin/accounts/{account['id']}", headers=admin_auth(admin_token))
    assert res.status_code == 200
    assert client.get("/api/admin/me", headers=admin_auth(token)).status_code == 401


def test_super_account_cannot_be_deleted(client, admin_token):
    accounts = client.get("/api/admin/accounts", headers=admin_auth(admin_token)).get_json()
    super_account = next(a for a in accounts if a["role"] == "super")
    res = client.delete(f"/api/admin/accounts/{super_account['id']}", headers=admin_auth(admin_token))
    assert res.status_code == 400
