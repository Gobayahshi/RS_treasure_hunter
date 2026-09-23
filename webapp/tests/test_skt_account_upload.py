"""SKT 자체 직원(RS팀) 계정을 엑셀로 일괄 등록하는 기능.

실제 사내 로스터 파일과 같은 모양(이름 / ID / PW / 구분·권한)을 그대로 올린다.
실제 ID(사번)는 숫자만이라 admins.username 대소문자 구분(기존 동작, 안 건드림)이 문제되지 않는다.
"""

import uuid
from io import BytesIO

from openpyxl import Workbook

from test_master_upload import upload  # 업로드 폼(체크박스 옵션 포함) 재사용


def sabeon() -> str:
    """테스트용 7자리 사번. 실제 사번처럼 숫자만 쓴다."""
    return str(1000000 + (uuid.uuid4().int % 9000000))


def admin_auth(token):
    return {"X-Admin-Token": token}


def rep_auth(token):
    return {"X-Rep-Token": token}


def build_skt_roster(rows, sheet_title="Sheet1"):
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_title
    ws.append(["이름", "ID", "PW", "구분/권한"])
    for row in rows:
        ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_roster_creates_super_and_staff_accounts(client, admin_token):
    admin_code = sabeon()
    staff_code = sabeon()
    data = build_skt_roster(
        [
            ["김총괄", admin_code, admin_code, "admin"],
            ["박직원", staff_code, staff_code, "SKT"],
        ]
    )
    res = upload(client, admin_token, data, filename="RS팀직원리스트.xlsx")
    assert res.status_code == 200, res.get_json()
    summary = res.get_json()["skt_accounts"]
    assert summary["created"] == 2, summary
    assert summary["errors"] == []

    # admin 표시 -> super, 총괄 권한. 초기 비번은 사번 그대로다.
    super_login = client.post(
        "/api/admin/login", json={"username": admin_code, "password": admin_code}
    ).get_json()
    assert super_login["role"] == "super"
    assert super_login["can_edit"] is True
    assert super_login["must_change_password"] is True
    H = admin_auth(super_login["token"])
    assert client.get("/api/reps", headers=H).status_code == 403  # 초기 비번부터 바꿔야 한다
    client.post(
        "/api/admin/change-password",
        json={"current_password": admin_code, "new_password": f"{admin_code}new"},
        headers=H,
    )
    assert client.get("/api/reps", headers=H).status_code == 200
    assert client.post(
        "/api/reps", json={"name": "x", "employee_code": sabeon()}, headers=H
    ).status_code == 201

    # SKT 표시 -> staff, 조회 전용
    staff_login = client.post(
        "/api/admin/login", json={"username": staff_code, "password": staff_code}
    ).get_json()
    assert staff_login["role"] == "staff"
    assert staff_login["can_edit"] is False


def test_staff_account_sees_all_dealers_in_inventory_but_cannot_upload(client, admin_token):
    staff_code = sabeon()
    upload(client, admin_token, build_skt_roster([["이직원", staff_code, staff_code, "SKT"]]))
    login = client.post("/api/admin/login", json={"username": staff_code, "password": staff_code}).get_json()
    H = admin_auth(login["token"])
    client.post(
        "/api/admin/change-password",
        json={"current_password": staff_code, "new_password": f"{staff_code}new"},
        headers=H,
    )
    inv = client.post("/api/inventory/login", json={"username": staff_code, "password": f"{staff_code}new"})
    assert inv.status_code == 200
    body = inv.get_json()
    assert body["can_see_all"] is True
    assert body["can_upload"] is False


def test_roster_name_is_stored_and_listed(client, admin_token):
    code = sabeon()
    upload(client, admin_token, build_skt_roster([["최이름", code, code, "SKT"]]))
    accounts = client.get("/api/admin/accounts", headers=admin_auth(admin_token)).get_json()
    row = next(a for a in accounts if a["username"] == code)
    assert row["name"] == "최이름"


def test_roster_strips_stray_whitespace_in_id(client, admin_token):
    code = sabeon()
    upload(client, admin_token, build_skt_roster([["공백테스트", f" {code} ", code, "SKT"]]))
    res = client.post("/api/admin/login", json={"username": code, "password": code})
    assert res.status_code == 200


def test_roster_skips_when_id_collides_with_existing_rep(client, admin_token, fixtures):
    data = build_skt_roster([["충돌", fixtures["employee_code"], fixtures["employee_code"], "SKT"]])
    res = upload(client, admin_token, data)
    summary = res.get_json()["skt_accounts"]
    assert summary["skipped"] == 1
    assert summary["created"] == 0
    assert "겹쳐서" in summary["errors"][0]


def test_reupload_updates_role_without_resetting_chosen_password(client, admin_token):
    code = sabeon()
    upload(client, admin_token, build_skt_roster([["승격전", code, code, "SKT"]]))
    login = client.post("/api/admin/login", json={"username": code, "password": code}).get_json()
    client.post(
        "/api/admin/change-password",
        json={"current_password": code, "new_password": "mychosen1"},
        headers=admin_auth(login["token"]),
    )

    # 다시 올리면서 admin 으로 승격
    upload(client, admin_token, build_skt_roster([["승격후", code, code, "admin"]]))

    # 이미 바꾼 비밀번호는 그대로 살아있다 (사번으로는 더 이상 로그인 안 됨)
    assert client.post(
        "/api/admin/login", json={"username": code, "password": code}
    ).status_code == 401
    relog = client.post("/api/admin/login", json={"username": code, "password": "mychosen1"})
    assert relog.status_code == 200
    assert relog.get_json()["role"] == "super"
    assert relog.get_json()["must_change_password"] is False


def test_roster_sheet_not_misclassified_as_reps(client, admin_token):
    """구분/권한 열이 있으면 ID/이름 열이 겹쳐도 reps 로 잘못 분류되지 않는다."""
    code = sabeon()
    res = upload(client, admin_token, build_skt_roster([["구분테스트", code, code, "SKT"]]))
    body = res.get_json()
    assert body["reps"]["created"] == 0
    assert body["skt_accounts"]["created"] == 1


def test_roster_requires_super(client, admin_token, rep_token):
    from test_accounts import create_staff

    _, staff_token = create_staff(client, admin_token)
    data = build_skt_roster([["x", sabeon(), "x", "SKT"]])
    assert upload(client, staff_token, data).status_code == 403
    assert upload(client, rep_token, data).status_code == 401
