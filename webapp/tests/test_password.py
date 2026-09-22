"""초기 비밀번호 강제 변경과 전화번호 기반 본인 재설정."""

import uuid
from datetime import datetime

import app as server_module  # noqa: F401  (conftest 가 먼저 DB_PATH 를 잡는다)


def rep_auth(token):
    return {"X-Rep-Token": token}


def admin_auth(token):
    return {"X-Admin-Token": token}


def make_rep(server, phone="010-1234-5678", password=None, dealer_id=None):
    """초기 비밀번호(=고유ID)를 쓰는 사원을 만든다."""
    from db import db_session

    code = uuid.uuid4().hex[:7]
    rep_id = uuid.uuid4().hex
    with db_session() as conn:
        if dealer_id is None:
            dealer_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO dealers (id, dealer_code, name, created_at) VALUES (?,?,?,?)",
                (dealer_id, f"D{uuid.uuid4().hex[:5]}", "테스트대리점", datetime.utcnow().isoformat()),
            )
        conn.execute(
            """
            INSERT INTO reps (id, dealer_id, name, employee_code, password_hash, created_at,
                              phone, must_change_password)
            VALUES (?,?,?,?,?,?,?,1)
            """,
            (
                rep_id,
                dealer_id,
                "홍길동",
                code,
                server_module.hash_password(password or code),
                datetime.utcnow().isoformat(),
                server_module.normalize_phone(phone),
            ),
        )
    return {"id": rep_id, "code": code, "phone": phone, "password": password or code}


# ---------------------------------------------------------------------------
# 초기 비밀번호 강제 변경
# ---------------------------------------------------------------------------


def test_initial_password_blocks_apps_until_changed(client, server):
    rep = make_rep(server)
    login = client.post(
        "/api/auth/login", json={"employee_code": rep["code"], "password": rep["password"]}
    ).get_json()
    assert login["must_change_password"] is True
    token = login["token"]

    # 보물찾기와 재고 화면 모두 막힌다
    blocked = client.get("/api/treasures/nearby?lat=37.5&lng=127.0", headers=rep_auth(token))
    assert blocked.status_code == 403
    assert blocked.get_json()["error"] == "PASSWORD_CHANGE_REQUIRED"
    assert client.get("/api/inventory/map", headers=admin_auth(token)).status_code == 403

    # 비밀번호를 바꾸는 길만 열려 있다
    assert client.get("/api/auth/me", headers=rep_auth(token)).status_code == 200
    assert client.get("/api/inventory/me", headers=admin_auth(token)).status_code == 200

    changed = client.post(
        "/api/auth/change-password",
        json={"current_password": rep["password"], "new_password": "newpw123"},
        headers=rep_auth(token),
    )
    assert changed.status_code == 200

    again = client.post(
        "/api/auth/login", json={"employee_code": rep["code"], "password": "newpw123"}
    ).get_json()
    assert again["must_change_password"] is False
    assert client.get(
        "/api/treasures/nearby?lat=37.5&lng=127.0", headers=rep_auth(again["token"])
    ).status_code == 200


def test_cannot_set_password_back_to_employee_code(client, server):
    """이미 바꾼 사람이 다시 고유ID를 비밀번호로 되돌리려는 경우."""
    rep = make_rep(server, password="mypw1234")
    token = client.post(
        "/api/auth/login", json={"employee_code": rep["code"], "password": "mypw1234"}
    ).get_json()["token"]
    res = client.post(
        "/api/auth/change-password",
        json={"current_password": "mypw1234", "new_password": rep["code"]},
        headers=rep_auth(token),
    )
    assert res.status_code == 400
    assert res.get_json()["error"] == "PASSWORD_TOO_SIMPLE"


def test_new_skt_staff_must_change_password(client, admin_token):
    username = f"skt{uuid.uuid4().hex[:6]}"
    client.post(
        "/api/admin/accounts",
        json={"username": username, "password": "temp1234"},
        headers=admin_auth(admin_token),
    )
    login = client.post(
        "/api/admin/login", json={"username": username, "password": "temp1234"}
    ).get_json()
    assert login["must_change_password"] is True
    H = admin_auth(login["token"])
    assert client.get("/api/reps", headers=H).status_code == 403

    assert client.post(
        "/api/admin/change-password",
        json={"current_password": "temp1234", "new_password": "sktnew123"},
        headers=H,
    ).status_code == 200
    assert client.get("/api/reps", headers=H).status_code == 200


# ---------------------------------------------------------------------------
# 전화번호로 본인 재설정
# ---------------------------------------------------------------------------


def test_reset_with_phone_sets_new_password(client, server):
    rep = make_rep(server, phone="010-1234-5678")
    res = client.post(
        "/api/auth/reset-password",
        json={"employee_code": rep["code"], "phone": "010-1234-5678", "new_password": "myown123"},
    )
    assert res.status_code == 200

    login = client.post(
        "/api/auth/login", json={"employee_code": rep["code"], "password": "myown123"}
    )
    assert login.status_code == 200
    # 본인이 정한 비밀번호라 강제 변경 대상이 아니다
    assert login.get_json()["must_change_password"] is False


def test_reset_accepts_any_phone_format(client, server):
    rep = make_rep(server, phone="01098765432")
    for typed in ("010-9876-5432", "010 9876 5432", "+82 10-9876-5432"):
        res = client.post(
            "/api/auth/reset-password",
            json={"employee_code": rep["code"], "phone": typed, "new_password": f"pw{typed[-4:]}"},
        )
        assert res.status_code == 200, typed


def test_reset_rejects_wrong_phone_and_unknown_id(client, server):
    rep = make_rep(server, phone="010-1234-5678")
    wrong = client.post(
        "/api/auth/reset-password",
        json={"employee_code": rep["code"], "phone": "010-0000-0000", "new_password": "hack1234"},
    )
    unknown = client.post(
        "/api/auth/reset-password",
        json={"employee_code": "nosuchid", "phone": "010-1234-5678", "new_password": "hack1234"},
    )
    assert wrong.status_code == 401
    assert unknown.status_code == 401
    # 고유ID가 있는지 없는지 알려주지 않는다
    assert wrong.get_json()["message"] == unknown.get_json()["message"]
    # 비밀번호는 그대로다
    assert client.post(
        "/api/auth/login", json={"employee_code": rep["code"], "password": rep["password"]}
    ).status_code == 200


def test_reset_without_registered_phone_is_refused(client, server):
    from db import db_session

    rep = make_rep(server)
    with db_session() as conn:
        conn.execute("UPDATE reps SET phone = NULL WHERE id = ?", (rep["id"],))
    res = client.post(
        "/api/auth/reset-password",
        json={"employee_code": rep["code"], "phone": "010-1234-5678", "new_password": "pw123456"},
    )
    assert res.status_code == 401


def test_reset_rate_limited_after_five_failures(client, server):
    rep = make_rep(server, phone="010-5555-6666")
    for _ in range(server.RESET_MAX_FAILURES):
        client.post(
            "/api/auth/reset-password",
            json={"employee_code": rep["code"], "phone": "010-0000-0000", "new_password": "pw123456"},
        )
    # 올바른 번호라도 잠시 막힌다
    blocked = client.post(
        "/api/auth/reset-password",
        json={"employee_code": rep["code"], "phone": "010-5555-6666", "new_password": "pw123456"},
    )
    assert blocked.status_code == 429
    assert blocked.get_json()["error"] == "TOO_MANY_ATTEMPTS"
    server._RESET_FAILURES.clear()


def test_reset_password_cannot_be_id_or_phone(client, server):
    rep = make_rep(server, phone="010-7777-8888")
    same_id = client.post(
        "/api/auth/reset-password",
        json={"employee_code": rep["code"], "phone": "010-7777-8888", "new_password": rep["code"]},
    )
    same_phone = client.post(
        "/api/auth/reset-password",
        json={"employee_code": rep["code"], "phone": "010-7777-8888", "new_password": "01077778888"},
    )
    assert same_id.status_code == 400
    assert same_phone.status_code == 400


def test_reset_ends_existing_sessions(client, server):
    rep = make_rep(server, password="oldpw123")
    token = client.post(
        "/api/auth/login", json={"employee_code": rep["code"], "password": "oldpw123"}
    ).get_json()["token"]
    assert client.get("/api/auth/me", headers=rep_auth(token)).status_code == 200

    client.post(
        "/api/auth/reset-password",
        json={"employee_code": rep["code"], "phone": rep["phone"], "new_password": "brandnew1"},
    )
    assert client.get("/api/auth/me", headers=rep_auth(token)).status_code == 401


def test_admin_rep_list_hides_full_phone(client, server, admin_token):
    rep = make_rep(server, phone="010-1234-5678")
    rows = client.get("/api/reps", headers=admin_auth(admin_token)).get_json()
    row = next(r for r in rows if r["employee_code"] == rep["code"])
    assert row["has_phone"] is True
    assert row["phone_masked"] == "010-****-5678"
    assert "phone" not in row
    assert "password_hash" not in row
