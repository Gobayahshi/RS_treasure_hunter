"""공지사항: SKT(총괄/직원)만 작성, 로그인한 모두(대리점 직원 포함) 읽기."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_accounts import admin_auth, create_staff  # noqa: E402


def rep_headers(client, fixtures):
    token = client.post(
        "/api/inventory/login",
        json={"username": fixtures["employee_code"], "password": fixtures["password"]},
    ).get_json()["token"]
    return {"X-Admin-Token": token}


def test_super_can_create_list_and_delete_notice(client, admin_token):
    headers = admin_auth(admin_token)
    created = client.post(
        "/api/notices",
        json={"title": "9월 정산 안내", "body": "9월 정산은 다음 주 진행됩니다."},
        headers=headers,
    )
    assert created.status_code == 201, created.get_json()
    notice = created.get_json()
    assert notice["author_role"] == "super"

    listed = client.get("/api/notices", headers=headers)
    assert listed.status_code == 200
    assert any(n["id"] == notice["id"] for n in listed.get_json())

    deleted = client.delete(f"/api/notices/{notice['id']}", headers=headers)
    assert deleted.status_code == 200
    listed_after = client.get("/api/notices", headers=headers).get_json()
    assert not any(n["id"] == notice["id"] for n in listed_after)


def test_staff_can_create_notice(client, admin_token):
    _, staff_token = create_staff(client, admin_token)
    res = client.post(
        "/api/notices",
        json={"title": "공지", "body": "직원도 작성 가능"},
        headers=admin_auth(staff_token),
    )
    assert res.status_code == 201
    assert res.get_json()["author_role"] == "staff"


def test_dealer_rep_can_read_but_not_write_notice(client, fixtures, admin_token):
    client.post(
        "/api/notices",
        json={"title": "전체 공지", "body": "대리점 직원도 봐야 함"},
        headers=admin_auth(admin_token),
    )
    headers = rep_headers(client, fixtures)
    listed = client.get("/api/notices", headers=headers)
    assert listed.status_code == 200
    assert len(listed.get_json()) >= 1

    blocked = client.post(
        "/api/notices",
        json={"title": "몰래 작성", "body": "안 되어야 함"},
        headers=headers,
    )
    assert blocked.status_code in (401, 403)


def test_missing_fields_rejected(client, admin_token):
    res = client.post("/api/notices", json={"title": "", "body": ""}, headers=admin_auth(admin_token))
    assert res.status_code == 400
    assert res.get_json()["error"] == "MISSING_FIELDS"
