"""직영점(D코드) 등록 API + 재고지도 판매점/직영점 토글, 판매점 목록 종류 필터 테스트."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_accounts import admin_auth, create_staff  # noqa: E402
from test_inventory import build_inventory_xlsx, upload_sample  # noqa: E402


def rep_headers(client, fixtures):
    token = client.post(
        "/api/inventory/login",
        json={"username": fixtures["employee_code"], "password": fixtures["password"]},
    ).get_json()["token"]
    return {"X-Admin-Token": token}


def _stub_geocode(monkeypatch, lat=37.55, lng=126.98):
    import app as server_module
    from geocode import GeocodeResult

    def _fake(address):
        if not address:
            return None
        return GeocodeResult(lat=lat, lng=lng, source="test", matched_address=address)

    monkeypatch.setattr(server_module, "geocode_address", _fake)
    return server_module


def test_dealer_rep_registers_retail_store_scoped_to_own_dealer(client, fixtures, monkeypatch):
    _stub_geocode(monkeypatch)
    headers = rep_headers(client, fixtures)
    res = client.post(
        "/api/inventory/retail-store",
        json={
            "store_code": "d999990001",
            "name": "테스트 직영점",
            "address": "서울시 강남구 테헤란로 1",
            # 신원 스푸핑 시도: 본인 대리점이 아닌 값을 보내도 무시되어야 한다.
            "dealer_code": "SOMEONE-ELSE",
        },
        headers=headers,
    )
    assert res.status_code == 201, res.get_json()
    body = res.get_json()
    assert body["geocoded"] is True
    store = body["store"]
    assert store["store_code"] == "D999990001"
    assert store["dealer_id"] == fixtures["dealer_id"]
    assert store["lat"] == 37.55 and store["lng"] == 126.98


def test_retail_store_rejects_partner_code(client, fixtures, monkeypatch):
    _stub_geocode(monkeypatch)
    headers = rep_headers(client, fixtures)
    res = client.post(
        "/api/inventory/retail-store",
        json={"store_code": "PABCDE", "name": "판매점인데 여기로 등록 시도", "address": "서울시 중구 1"},
        headers=headers,
    )
    assert res.status_code == 400
    assert res.get_json()["error"] == "NOT_RETAIL_CODE"


def test_retail_store_super_must_pick_dealer(client, admin_token, fixtures, monkeypatch):
    _stub_geocode(monkeypatch)
    headers = admin_auth(admin_token)
    missing = client.post(
        "/api/inventory/retail-store",
        json={"store_code": "D999990002", "name": "총괄 등록", "address": "서울시 종로구 1"},
        headers=headers,
    )
    assert missing.status_code == 400
    assert missing.get_json()["error"] == "DEALER_CODE_REQUIRED"

    with_dealer_code = _dealer_code(client, headers, fixtures["dealer_id"])
    ok = client.post(
        "/api/inventory/retail-store",
        json={
            "store_code": "D999990002",
            "name": "총괄 등록",
            "address": "서울시 종로구 1",
            "dealer_code": with_dealer_code,
        },
        headers=headers,
    )
    assert ok.status_code == 201, ok.get_json()
    assert ok.get_json()["store"]["dealer_id"] == fixtures["dealer_id"]


def _dealer_code(client, headers, dealer_id):
    dealers = client.get("/api/dealers", headers=headers).get_json()
    row = next(d for d in dealers if d["id"] == dealer_id)
    return row["dealer_code"]


def test_skt_staff_cannot_register_retail_store(client, admin_token, fixtures, monkeypatch):
    _stub_geocode(monkeypatch)
    _, staff_token = create_staff(client, admin_token)
    dealer_code = _dealer_code(client, admin_auth(admin_token), fixtures["dealer_id"])
    res = client.post(
        "/api/inventory/retail-store",
        json={
            "store_code": "D999990003",
            "name": "직원 등록 시도",
            "address": "서울시 마포구 1",
            "dealer_code": dealer_code,
        },
        headers=admin_auth(staff_token),
    )
    assert res.status_code == 403
    assert res.get_json()["error"] == "VIEW_ONLY"


def test_retail_store_saved_even_when_geocode_fails(client, fixtures, monkeypatch):
    import app as server_module

    monkeypatch.setattr(server_module, "geocode_address", lambda address: None)
    headers = rep_headers(client, fixtures)
    res = client.post(
        "/api/inventory/retail-store",
        json={"store_code": "D999990004", "name": "좌표 실패", "address": "존재하지 않는 주소 123"},
        headers=headers,
    )
    assert res.status_code == 201
    body = res.get_json()
    assert body["geocoded"] is False
    assert body["store"]["lat"] == 0.0 and body["store"]["lng"] == 0.0


def test_store_list_type_filter(client, admin_token, fixtures, monkeypatch):
    _stub_geocode(monkeypatch)
    headers = rep_headers(client, fixtures)
    client.post(
        "/api/inventory/retail-store",
        json={"store_code": "D999990005", "name": "필터 테스트 직영점", "address": "서울시 서초구 1"},
        headers=headers,
    )
    admin_headers = admin_auth(admin_token)
    # q 없이 type 만으로도 걸러져야 한다
    retail_only = client.get("/api/stores?type=retail&limit=200", headers=admin_headers)
    assert retail_only.status_code == 200
    codes = [s["store_code"] for s in retail_only.get_json()["items"]]
    assert "D999990005" in codes
    assert fixtures["store_code"] not in codes

    partner_only = client.get("/api/stores?type=partner&limit=200", headers=admin_headers)
    partner_codes = [s["store_code"] for s in partner_only.get_json()["items"]]
    assert fixtures["store_code"] in partner_codes
    assert "D999990005" not in partner_codes


def test_include_partner_toggle_filters_out_partner_stores(client, fixtures, admin_token):
    # 세션 전체가 공유하는 테스트 DB라 다른 테스트의 대리점/재고까지 섞이지 않도록
    # dealer_id로 이 테스트의 대리점만 본다.
    upload_sample(fixtures)
    headers = admin_auth(admin_token)
    only_retail = client.get(
        "/api/inventory/map",
        query_string={
            "model": "ALL",
            "include_partner": "0",
            "include_retail": "1",
            "dealer_id": fixtures["dealer_id"],
        },
        headers=headers,
    )
    assert only_retail.status_code == 200
    data = only_retail.get_json()
    assert data["total_qty"] == 0
    assert data["include_partner"] is False

    with_partner = client.get(
        "/api/inventory/map",
        query_string={"model": "ALL", "dealer_id": fixtures["dealer_id"]},
        headers=headers,
    )
    assert with_partner.get_json()["total_qty"] == 2
    assert with_partner.get_json()["include_partner"] is True
