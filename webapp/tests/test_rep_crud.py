"""관리자가 영업사원 계정을 직접 추가·수정·삭제하는 화면의 API."""

import uuid
from datetime import datetime


def admin_auth(token):
    return {"X-Admin-Token": token}


def rep_auth(token):
    return {"X-Rep-Token": token}


def make_dealer(code=None, name="추가테스트대리점"):
    from db import db_session

    code = code or f"D{uuid.uuid4().hex[:5]}"
    with db_session() as conn:
        dealer_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO dealers (id, dealer_code, name, created_at) VALUES (?,?,?,?)",
            (dealer_id, code, name, datetime.utcnow().isoformat()),
        )
    return dealer_id, code


# ---------------------------------------------------------------------------
# 추가 (POST /api/reps)
# ---------------------------------------------------------------------------


def test_super_can_add_rep_with_dealer_and_role(client, admin_token):
    _, dealer_code = make_dealer()
    code = f"NEW{uuid.uuid4().hex[:6].upper()}"
    res = client.post(
        "/api/reps",
        json={"name": "새사람", "employee_code": code.lower(), "dealer_code": dealer_code, "dealer_role": "manager"},
        headers=admin_auth(admin_token),
    )
    assert res.status_code == 201
    body = res.get_json()
    assert body["employee_code"] == code  # 대문자로 저장
    assert body["dealer_code"] == dealer_code
    assert body["dealer_role"] == "manager"
    assert body["must_change_password"] is True

    # 초기 비밀번호 = 고유ID (대소문자 무시 로그인)
    login = client.post("/api/auth/login", json={"employee_code": code.lower(), "password": code})
    assert login.status_code == 200


def test_add_rep_with_phone_last4_enables_self_reset(client, admin_token):
    code = f"PH{uuid.uuid4().hex[:6].upper()}"
    res = client.post(
        "/api/reps",
        json={"name": "전화번호", "employee_code": code, "phone_last4": "010-1234-5678"},
        headers=admin_auth(admin_token),
    )
    assert res.status_code == 201
    body = res.get_json()
    assert body["has_phone"] is True
    assert body["phone_masked"] == "****-5678"  # 뒤 4자리만 저장, 전체 번호는 API로 내려가지 않음
    assert "phone_last4" not in body

    reset = client.post(
        "/api/auth/reset-password",
        json={"employee_code": code, "phone": "5678", "new_password": "newpw123"},
    )
    assert reset.status_code == 200


def test_add_rep_without_phone_cannot_self_reset(client, admin_token):
    code = f"NOPHONE{uuid.uuid4().hex[:5].upper()}"
    client.post("/api/reps", json={"name": "번호없음", "employee_code": code}, headers=admin_auth(admin_token))
    reset = client.post(
        "/api/auth/reset-password",
        json={"employee_code": code, "phone": "0000", "new_password": "newpw123"},
    )
    assert reset.status_code == 401


def test_add_rep_without_dealer_leaves_unassigned(client, admin_token):
    code = f"NODEALER{uuid.uuid4().hex[:5].upper()}"
    res = client.post(
        "/api/reps", json={"name": "무소속", "employee_code": code}, headers=admin_auth(admin_token)
    )
    assert res.status_code == 201
    assert res.get_json()["dealer_id"] is None or res.get_json()["dealer_id"] == ""


def test_add_rep_rejects_id_colliding_with_skt_account(client, admin_token):
    username = f"skt{uuid.uuid4().hex[:6]}"
    client.post(
        "/api/admin/accounts", json={"username": username, "password": "temp1234"}, headers=admin_auth(admin_token)
    )
    res = client.post(
        "/api/reps", json={"name": "충돌", "employee_code": username}, headers=admin_auth(admin_token)
    )
    assert res.status_code == 409
    assert res.get_json()["error"] == "EMPLOYEE_CODE_EXISTS"


def test_add_rep_rejects_bad_dealer_role(client, admin_token):
    code = f"BADROLE{uuid.uuid4().hex[:5].upper()}"
    res = client.post(
        "/api/reps",
        json={"name": "x", "employee_code": code, "dealer_role": "owner"},
        headers=admin_auth(admin_token),
    )
    assert res.status_code == 400


def test_add_rep_requires_super_not_staff_or_rep(client, admin_token, rep_token):
    from test_accounts import create_staff

    _, staff_token = create_staff(client, admin_token)
    body = {"name": "x", "employee_code": f"X{uuid.uuid4().hex[:6]}"}
    assert client.post("/api/reps", json=body, headers=admin_auth(staff_token)).status_code == 403
    assert client.post("/api/reps", json=body, headers=admin_auth(rep_token)).status_code == 401
    assert client.post("/api/reps", json=body).status_code == 401


# ---------------------------------------------------------------------------
# 수정 (PATCH /api/reps/<id>)
# ---------------------------------------------------------------------------


def test_super_can_edit_id_name_dealer_role(client, admin_token, fixtures):
    _, dealer_code = make_dealer(name="옮길대리점")
    new_code = f"RENAMED{uuid.uuid4().hex[:5].upper()}"
    res = client.patch(
        f"/api/reps/{fixtures['rep_id']}",
        json={"name": "새이름", "employee_code": new_code, "dealer_code": dealer_code, "dealer_role": "manager"},
        headers=admin_auth(admin_token),
    )
    assert res.status_code == 200
    body = res.get_json()
    assert body["name"] == "새이름"
    assert body["employee_code"] == new_code
    assert body["dealer_code"] == dealer_code
    assert body["dealer_role"] == "manager"

    # 바뀐 고유ID로 로그인된다. 기존 것으로는 안 된다.
    assert client.post(
        "/api/auth/login", json={"employee_code": new_code, "password": fixtures["password"]}
    ).status_code == 200
    assert client.post(
        "/api/auth/login", json={"employee_code": fixtures["employee_code"], "password": fixtures["password"]}
    ).status_code == 404


def test_edit_can_unassign_dealer(client, admin_token, fixtures):
    res = client.patch(
        f"/api/reps/{fixtures['rep_id']}", json={"dealer_code": ""}, headers=admin_auth(admin_token)
    )
    assert res.status_code == 200
    assert not res.get_json()["dealer_id"]


def test_edit_can_set_phone_last4(client, admin_token, fixtures):
    res = client.patch(
        f"/api/reps/{fixtures['rep_id']}",
        json={"phone_last4": "010-9999-4321"},
        headers=admin_auth(admin_token),
    )
    assert res.status_code == 200
    body = res.get_json()
    assert body["has_phone"] is True
    assert body["phone_masked"] == "****-4321"

    reset = client.post(
        "/api/auth/reset-password",
        json={"employee_code": fixtures["employee_code"], "phone": "4321", "new_password": "resetpw12"},
    )
    assert reset.status_code == 200


def test_edit_rejects_bad_phone_last4(client, admin_token, fixtures):
    res = client.patch(
        f"/api/reps/{fixtures['rep_id']}",
        json={"phone_last4": "ab"},
        headers=admin_auth(admin_token),
    )
    assert res.status_code == 400


def test_edit_rejects_duplicate_employee_code(client, admin_token, fixtures):
    res = client.patch(
        f"/api/reps/{fixtures['rep_id']}",
        json={"employee_code": fixtures["other_employee_code"]},
        headers=admin_auth(admin_token),
    )
    assert res.status_code == 409
    assert res.get_json()["error"] == "EMPLOYEE_CODE_EXISTS"


def test_edit_reset_password_forces_change(client, admin_token, fixtures, rep_token):
    # 픽스처의 고유ID는 테스트 편의상 소문자다. 실제 데이터는 항상 대문자로 저장되므로
    # (create_rep/excel_import 모두 저장 전에 정규화한다) 정규화된 형태로 맞춰 둔다.
    client.patch(
        f"/api/reps/{fixtures['rep_id']}",
        json={"employee_code": fixtures["employee_code"].upper()},
        headers=admin_auth(admin_token),
    )

    # 먼저 초기 비밀번호를 바꿔서 must_change_password 를 꺼 둔다.
    client.post(
        "/api/auth/change-password",
        json={"current_password": fixtures["password"], "new_password": "chosenpw1"},
        headers=rep_auth(rep_token),
    )
    assert client.get(
        "/api/treasures/nearby?lat=37.5&lng=127.0", headers=rep_auth(rep_token)
    ).status_code == 200

    res = client.patch(
        f"/api/reps/{fixtures['rep_id']}", json={"reset_password": True}, headers=admin_auth(admin_token)
    )
    assert res.status_code == 200
    assert res.get_json()["must_change_password"] is True

    # 예전 비밀번호는 더 이상 안 되고, 고유ID(대문자 정규화 형태)로는 된다.
    # 초기/재설정 비밀번호는 항상 고유ID의 대문자 정규화 형태다 (excel_import 의 관례와 동일).
    assert client.post(
        "/api/auth/login", json={"employee_code": fixtures["employee_code"], "password": "chosenpw1"}
    ).status_code == 401
    canonical_code = fixtures["employee_code"].upper()
    login = client.post(
        "/api/auth/login", json={"employee_code": fixtures["employee_code"], "password": canonical_code}
    )
    assert login.status_code == 200
    assert login.get_json()["must_change_password"] is True

    # 기존 세션 토큰 자체는 안 끊겼지만(로그아웃되지 않았지만), 초기 비번 상태가 됐으니
    # 비밀번호를 바꾸기 전까지는 다른 API 가 막힌다.
    blocked = client.get("/api/treasures/nearby?lat=37.5&lng=127.0", headers=rep_auth(rep_token))
    assert blocked.status_code == 403
    assert blocked.get_json()["error"] == "PASSWORD_CHANGE_REQUIRED"


def test_edit_with_no_fields_is_rejected(client, admin_token, fixtures):
    res = client.patch(f"/api/reps/{fixtures['rep_id']}", json={}, headers=admin_auth(admin_token))
    assert res.status_code == 400


def test_edit_missing_rep_404(client, admin_token):
    res = client.patch(
        f"/api/reps/{uuid.uuid4().hex}", json={"name": "x"}, headers=admin_auth(admin_token)
    )
    assert res.status_code == 404


def test_edit_requires_super(client, admin_token, rep_token, fixtures):
    from test_accounts import create_staff

    _, staff_token = create_staff(client, admin_token)
    body = {"name": "x"}
    assert client.patch(f"/api/reps/{fixtures['rep_id']}", json=body, headers=admin_auth(staff_token)).status_code == 403
    assert client.patch(f"/api/reps/{fixtures['rep_id']}", json=body, headers=admin_auth(rep_token)).status_code == 401


# ---------------------------------------------------------------------------
# 삭제 (DELETE /api/reps/<id>)
# ---------------------------------------------------------------------------


def test_super_can_delete_rep_and_records(client, admin_token, fixtures, rep_token):
    from conftest import complete_visit
    from db import db_session

    complete_visit(client, rep_token, fixtures["store_id"])
    res = client.delete(f"/api/reps/{fixtures['rep_id']}", headers=admin_auth(admin_token))
    assert res.status_code == 200
    with db_session() as conn:
        assert conn.execute("SELECT 1 FROM reps WHERE id = ?", (fixtures["rep_id"],)).fetchone() is None
        assert conn.execute(
            "SELECT COUNT(*) c FROM point_ledger WHERE rep_id = ?", (fixtures["rep_id"],)
        ).fetchone()["c"] == 0

    # 삭제 뒤엔 그 세션 토큰도 더 이상 안 먹는다
    assert client.get("/api/auth/me", headers=rep_auth(rep_token)).status_code == 401


def test_delete_missing_rep_404(client, admin_token):
    assert client.delete(f"/api/reps/{uuid.uuid4().hex}", headers=admin_auth(admin_token)).status_code == 404


def test_delete_requires_super(client, admin_token, rep_token, fixtures):
    from test_accounts import create_staff

    _, staff_token = create_staff(client, admin_token)
    assert client.delete(f"/api/reps/{fixtures['rep_id']}", headers=admin_auth(staff_token)).status_code == 403
    assert client.delete(f"/api/reps/{fixtures['rep_id']}", headers=admin_auth(rep_token)).status_code == 401
    assert client.delete(f"/api/reps/{fixtures['rep_id']}").status_code == 401
