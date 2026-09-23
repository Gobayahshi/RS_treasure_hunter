"""관리자가 대리점을 직접 삭제하는 API. 소속 사원과 그 기록도 함께 사라진다."""

import uuid
from datetime import datetime


def admin_auth(token):
    return {"X-Admin-Token": token}


def rep_auth(token):
    return {"X-Rep-Token": token}


def make_dealer(code=None, name="검색용대리점"):
    from db import db_session

    code = code or f"D{uuid.uuid4().hex[:5]}"
    with db_session() as conn:
        dealer_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO dealers (id, dealer_code, name, created_at) VALUES (?,?,?,?)",
            (dealer_id, code, name, datetime.utcnow().isoformat()),
        )
    return dealer_id, code


def test_super_can_delete_dealer_with_reps_and_records(client, admin_token, fixtures, rep_token):
    from conftest import complete_visit
    from db import db_session

    complete_visit(client, rep_token, fixtures["store_id"])

    res = client.delete(f"/api/dealers/{fixtures['dealer_id']}", headers=admin_auth(admin_token))
    assert res.status_code == 200
    body = res.get_json()
    assert body["reps_removed"] == 2  # fixtures 는 사원 2명(rep_id, other_rep_id)을 만든다
    assert body["stores_unassigned"] == 1

    with db_session() as conn:
        assert conn.execute("SELECT 1 FROM dealers WHERE id = ?", (fixtures["dealer_id"],)).fetchone() is None
        assert conn.execute("SELECT 1 FROM reps WHERE dealer_id = ?", (fixtures["dealer_id"],)).fetchone() is None
        assert conn.execute(
            "SELECT COUNT(*) c FROM point_ledger WHERE rep_id = ?", (fixtures["rep_id"],)
        ).fetchone()["c"] == 0
        store = conn.execute("SELECT dealer_id FROM stores WHERE id = ?", (fixtures["store_id"],)).fetchone()
        assert store["dealer_id"] is None

    # 삭제된 대리점 소속 사원의 토큰도 더는 안 먹는다
    assert client.get("/api/auth/me", headers=rep_auth(rep_token)).status_code == 401


def test_delete_dealer_removes_its_inventory(client, admin_token, fixtures):
    from db import db_session

    upload_id = uuid.uuid4().hex
    item_id = uuid.uuid4().hex
    now = datetime.utcnow().isoformat()
    with db_session() as conn:
        conn.execute(
            "INSERT INTO inventory_uploads (id, filename, row_count, created_at, dealer_id)"
            " VALUES (?,?,?,?,?)",
            (upload_id, "test.xlsx", 1, now, fixtures["dealer_id"]),
        )
        conn.execute(
            "INSERT INTO inventory_items (id, upload_id, store_code, holder_type, dealer_id)"
            " VALUES (?,?,?,?,?)",
            (item_id, upload_id, fixtures["store_code"], "partner", fixtures["dealer_id"]),
        )

    res = client.delete(f"/api/dealers/{fixtures['dealer_id']}", headers=admin_auth(admin_token))
    assert res.status_code == 200

    with db_session() as conn:
        assert conn.execute("SELECT 1 FROM inventory_uploads WHERE id = ?", (upload_id,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM inventory_items WHERE id = ?", (item_id,)).fetchone() is None


def test_delete_missing_dealer_404(client, admin_token):
    res = client.delete(f"/api/dealers/{uuid.uuid4().hex}", headers=admin_auth(admin_token))
    assert res.status_code == 404
    assert res.get_json()["error"] == "DEALER_NOT_FOUND"


def test_delete_dealer_requires_super(client, admin_token, rep_token, fixtures):
    from test_accounts import create_staff

    _, staff_token = create_staff(client, admin_token)
    assert client.delete(
        f"/api/dealers/{fixtures['dealer_id']}", headers=admin_auth(staff_token)
    ).status_code == 403
    assert client.delete(
        f"/api/dealers/{fixtures['dealer_id']}", headers=admin_auth(rep_token)
    ).status_code == 401
    assert client.delete(f"/api/dealers/{fixtures['dealer_id']}").status_code == 401


# ---------------------------------------------------------------------------
# 수정 (PATCH /api/dealers/<id>)
# ---------------------------------------------------------------------------


def test_super_can_rename_dealer(client, admin_token, fixtures):
    new_code = f"REN{uuid.uuid4().hex[:6].upper()}"
    res = client.patch(
        f"/api/dealers/{fixtures['dealer_id']}",
        json={"name": "새대리점이름", "dealer_code": new_code},
        headers=admin_auth(admin_token),
    )
    assert res.status_code == 200
    body = res.get_json()
    assert body["name"] == "새대리점이름"
    assert body["dealer_code"] == new_code


def test_update_dealer_rejects_duplicate_code(client, admin_token, fixtures):
    _, other_code = make_dealer()
    res = client.patch(
        f"/api/dealers/{fixtures['dealer_id']}",
        json={"dealer_code": other_code},
        headers=admin_auth(admin_token),
    )
    assert res.status_code == 409
    assert res.get_json()["error"] == "DEALER_CODE_EXISTS"


def test_update_dealer_with_no_fields_is_rejected(client, admin_token, fixtures):
    res = client.patch(f"/api/dealers/{fixtures['dealer_id']}", json={}, headers=admin_auth(admin_token))
    assert res.status_code == 400


def test_update_missing_dealer_404(client, admin_token):
    res = client.patch(
        f"/api/dealers/{uuid.uuid4().hex}", json={"name": "x"}, headers=admin_auth(admin_token)
    )
    assert res.status_code == 404


def test_update_dealer_requires_super(client, admin_token, rep_token, fixtures):
    from test_accounts import create_staff

    _, staff_token = create_staff(client, admin_token)
    body = {"name": "x"}
    assert client.patch(
        f"/api/dealers/{fixtures['dealer_id']}", json=body, headers=admin_auth(staff_token)
    ).status_code == 403
    assert client.patch(
        f"/api/dealers/{fixtures['dealer_id']}", json=body, headers=admin_auth(rep_token)
    ).status_code == 401
