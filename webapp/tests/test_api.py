"""API 권한, 검토 대기 처리, 리워드 차감 테스트."""

from conftest import complete_visit


def auth(token):
    return {"X-Rep-Token": token}


def admin_auth(token):
    return {"X-Admin-Token": token}


# ---------------------------------------------------------------------------
# 영업사원 인증
# ---------------------------------------------------------------------------


def test_rep_endpoints_require_token(client, fixtures):
    paths = [
        ("get", "/api/treasures/nearby?lat=37.5&lng=127.0"),
        ("get", f"/api/points/{fixtures['rep_id']}"),
        ("get", f"/api/rewards/{fixtures['rep_id']}"),
        ("get", f"/api/reps/{fixtures['rep_id']}"),
        ("get", "/api/stats/rankings"),
    ]
    for method, path in paths:
        res = getattr(client, method)(path)
        assert res.status_code == 401, path
        assert res.get_json()["error"] == "REP_AUTH_REQUIRED"

    res = client.post("/api/visit-sessions", json={"rep_id": fixtures["rep_id"], "store_id": fixtures["store_id"]})
    assert res.status_code == 401


def test_login_returns_token_without_password_hash(client, fixtures):
    res = client.post(
        "/api/auth/login",
        json={"employee_code": fixtures["employee_code"], "password": fixtures["password"]},
    )
    body = res.get_json()
    assert res.status_code == 200
    assert body["token"]
    assert "password_hash" not in body


def test_visit_session_belongs_to_token_owner(client, fixtures, rep_token):
    """본문에 남의 rep_id를 넣어도 토큰 주인의 세션으로 만들어진다."""
    from db import db_session

    res = client.post(
        "/api/visit-sessions",
        json={"rep_id": fixtures["other_rep_id"], "store_id": fixtures["store_id"]},
        headers=auth(rep_token),
    )
    session_id = res.get_json()["id"]
    with db_session() as conn:
        owner = conn.execute(
            "SELECT rep_id FROM visit_sessions WHERE id = ?", (session_id,)
        ).fetchone()["rep_id"]
    assert owner == fixtures["rep_id"]


def test_other_rep_cannot_touch_my_session(client, fixtures, rep_token):
    session_id = client.post(
        "/api/visit-sessions",
        json={"store_id": fixtures["store_id"]},
        headers=auth(rep_token),
    ).get_json()["id"]
    other = client.post(
        "/api/auth/login",
        json={"employee_code": fixtures["other_employee_code"], "password": fixtures["password"]},
    ).get_json()["token"]

    res = client.post(
        f"/api/visit-sessions/{session_id}/samples",
        json={"lat": 37.5, "lng": 127.0, "accuracy": 10},
        headers=auth(other),
    )
    assert res.status_code == 404
    assert client.post(f"/api/visit-sessions/{session_id}/complete", headers=auth(other)).status_code == 404


def test_cannot_read_other_reps_points(client, fixtures, rep_token):
    assert client.get(f"/api/points/{fixtures['other_rep_id']}", headers=auth(rep_token)).status_code == 403
    assert client.get(f"/api/reps/{fixtures['other_rep_id']}", headers=auth(rep_token)).status_code == 403
    assert client.get(f"/api/points/{fixtures['rep_id']}", headers=auth(rep_token)).status_code == 200


def test_logout_invalidates_token(client, rep_token):
    assert client.get("/api/auth/me", headers=auth(rep_token)).status_code == 200
    assert client.post("/api/auth/logout", headers=auth(rep_token)).status_code == 200
    assert client.get("/api/auth/me", headers=auth(rep_token)).status_code == 401


def test_change_password_wrong_current_is_not_session_error(client, rep_token):
    res = client.post(
        "/api/auth/change-password",
        json={"current_password": "wrong", "new_password": "new1234"},
        headers=auth(rep_token),
    )
    assert res.status_code == 401
    # 세션 만료(REP_AUTH_REQUIRED)와 구분돼야 화면이 로그아웃시키지 않는다.
    assert res.get_json()["error"] == "INVALID_PASSWORD"


# ---------------------------------------------------------------------------
# 방문 인증 / 검토 대기 처리
# ---------------------------------------------------------------------------


def test_good_visit_earns_points(client, fixtures, rep_token):
    _, result = complete_visit(client, rep_token, fixtures["store_id"], accuracy=10)
    assert result["evaluation"]["status"] == "auto_approved"
    assert result["point_ledger_entry"]["points"] > 0
    points = client.get(f"/api/points/{fixtures['rep_id']}", headers=auth(rep_token)).get_json()
    assert points["total"] == result["point_ledger_entry"]["points"]
    assert points["balance"] == points["total"]


def test_points_include_dealer_rank(client, fixtures, rep_token):
    complete_visit(client, rep_token, fixtures["store_id"], accuracy=10)
    mine = client.get(f"/api/points/{fixtures['rep_id']}", headers=auth(rep_token)).get_json()
    assert mine["dealer_rep_count"] == 2
    assert mine["dealer_rank"] == 1

    other_token = client.post(
        "/api/auth/login",
        json={"employee_code": fixtures["other_employee_code"], "password": fixtures["password"]},
    ).get_json()["token"]
    other = client.get(f"/api/points/{fixtures['other_rep_id']}", headers=auth(other_token)).get_json()
    assert other["dealer_rep_count"] == 2
    assert other["dealer_rank"] == 2


def test_points_dealer_rank_null_without_dealer(client, server):
    import uuid
    from datetime import datetime
    from db import db_session

    code = uuid.uuid4().hex[:7]
    rep_id = uuid.uuid4().hex
    with db_session() as conn:
        conn.execute(
            "INSERT INTO reps (id, dealer_id, name, employee_code, password_hash, created_at)"
            " VALUES (?, NULL, '무소속', ?, ?, ?)",
            (rep_id, code, server.hash_password("pw1234"), datetime.utcnow().isoformat()),
        )
    login = client.post("/api/auth/login", json={"employee_code": code, "password": "pw1234"}).get_json()
    assert login["id"] == rep_id
    points = client.get(f"/api/points/{rep_id}", headers=auth(login["token"])).get_json()
    assert points["dealer_rank"] is None
    assert points["dealer_rep_count"] is None


def test_pending_review_can_be_approved(client, fixtures, rep_token, admin_token):
    session_id, result = complete_visit(client, rep_token, fixtures["store_id"], accuracy=250)
    assert result["evaluation"]["status"] == "pending_review"
    assert result["point_ledger_entry"] is None

    queue = client.get("/api/admin/visit-sessions", headers=admin_auth(admin_token)).get_json()
    ids = [item["id"] for item in queue["items"]]
    assert session_id in ids
    item = next(i for i in queue["items"] if i["id"] == session_id)
    assert item["rep_name"] == "홍길동"
    assert item["first_sample_distance_m"] == 0
    assert [r["code"] for r in item["reasons"]] == ["R3_LOW_GPS_ACCURACY"]

    approved = client.post(
        f"/api/admin/visit-sessions/{session_id}/review",
        json={"decision": "approve"},
        headers=admin_auth(admin_token),
    )
    assert approved.status_code == 200
    body = approved.get_json()
    assert body["session"]["status"] == "manual_approved"
    assert body["session"]["reviewed_by"] == "admin"
    assert body["point_ledger_entry"]["points"] > 0

    # 두 번 승인해서 포인트가 두 번 나가지 않는다
    again = client.post(
        f"/api/admin/visit-sessions/{session_id}/review",
        json={"decision": "approve"},
        headers=admin_auth(admin_token),
    )
    assert again.status_code == 409


def test_pending_review_reject_gives_no_points(client, fixtures, rep_token, admin_token):
    session_id, _ = complete_visit(client, rep_token, fixtures["store_id"], accuracy=250)
    before = client.get(f"/api/points/{fixtures['rep_id']}", headers=auth(rep_token)).get_json()["total"]
    res = client.post(
        f"/api/admin/visit-sessions/{session_id}/review",
        json={"decision": "reject"},
        headers=admin_auth(admin_token),
    )
    assert res.get_json()["session"]["status"] == "manual_rejected"
    after = client.get(f"/api/points/{fixtures['rep_id']}", headers=auth(rep_token)).get_json()["total"]
    assert after == before


def test_review_requires_admin(client, fixtures, rep_token):
    assert client.get("/api/admin/visit-sessions").status_code == 401
    assert client.get("/api/admin/visit-sessions", headers=auth(rep_token)).status_code == 401


# ---------------------------------------------------------------------------
# 리워드 포인트 차감
# ---------------------------------------------------------------------------


def test_reward_request_deducts_balance(client, fixtures, rep_token):
    _, result = complete_visit(client, rep_token, fixtures["store_id"])
    earned = result["point_ledger_entry"]["points"]

    first = client.post(
        "/api/rewards", json={"type": "기프티콘", "point_cost": earned}, headers=auth(rep_token)
    )
    assert first.status_code == 201
    assert first.get_json()["wallet"]["balance"] == 0

    # 같은 포인트로 다시 신청할 수 없다
    second = client.post(
        "/api/rewards", json={"type": "기프티콘", "point_cost": earned}, headers=auth(rep_token)
    )
    assert second.status_code == 400
    assert second.get_json()["error"] == "INSUFFICIENT_POINTS"

    wallet = client.get(f"/api/points/{fixtures['rep_id']}", headers=auth(rep_token)).get_json()
    assert wallet["total"] == earned
    assert wallet["used"] == earned
    assert wallet["balance"] == 0


# ---------------------------------------------------------------------------
# 판매점 검색 (관리자)
# ---------------------------------------------------------------------------


def test_store_search_returns_paged_shape(client, admin_token, fixtures):
    H = admin_auth(admin_token)
    res = client.get("/api/stores", headers=H)
    assert res.status_code == 200
    body = res.get_json()
    assert set(["items", "total", "matched", "limit", "query"]) <= set(body.keys())
    assert body["total"] >= 1
    assert len(body["items"]) <= body["limit"]


def test_store_search_by_code_and_no_match(client, admin_token, fixtures):
    H = admin_auth(admin_token)

    found = client.get(f"/api/stores?q={fixtures['store_code']}", headers=H).get_json()
    assert found["matched"] >= 1
    assert any(s["store_code"] == fixtures["store_code"] for s in found["items"])

    empty = client.get("/api/stores?q=no-such-store-zzz", headers=H).get_json()
    assert empty["items"] == []
    assert empty["matched"] == 0


def test_cancelled_reward_returns_points(client, fixtures, rep_token):
    from db import db_session

    _, result = complete_visit(client, rep_token, fixtures["store_id"])
    earned = result["point_ledger_entry"]["points"]
    reward_id = client.post(
        "/api/rewards", json={"type": "커피", "point_cost": earned}, headers=auth(rep_token)
    ).get_json()["id"]
    with db_session() as conn:
        conn.execute("UPDATE rewards SET status = 'cancelled' WHERE id = ?", (reward_id,))
    wallet = client.get(f"/api/points/{fixtures['rep_id']}", headers=auth(rep_token)).get_json()
    assert wallet["balance"] == earned
