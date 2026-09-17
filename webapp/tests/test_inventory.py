"""재고 엑셀 파싱과 지도 집계 테스트."""

import os
import sys
from io import BytesIO

import pytest
from openpyxl import Workbook

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from excel_import import normalize_store_code  # noqa: E402
from inventory import (  # noqa: E402
    address_region,
    classify_holder,
    inventory_dealer_roster,
    inventory_map_points,
    inventory_model_breakdown,
    inventory_model_catalog,
    inventory_overview,
    inventory_store_price_sum,
    parse_inventory_file,
    replace_inventory,
)


def build_inventory_xlsx(rows, as_of="기준일자: 2026-09-16"):
    """실제 재고현황 엑셀과 같은 모양(제목행 + 헤더행 + 데이터)으로 만든다."""
    wb = Workbook()
    ws = wb.active
    ws.append([as_of])
    ws.append(
        ["보유처매장코드", "보유처", "대표상품명", "모델명", "실구매가", "보유기간", "일련번호", "레벨0조직명"]
    )
    for row in rows:
        ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_normalize_store_code():
    assert normalize_store_code(" pe2810 ") == "PE2810"
    assert normalize_store_code("pe 2810") == "PE2810"
    assert normalize_store_code(None) == ""


def test_classify_holder():
    assert classify_holder("PE2810") == "partner"  # 판매점
    assert classify_holder("D1474601") == "retail"  # 직영점
    assert classify_holder("D14746") == "hq"  # 대리점 본사
    assert classify_holder("X123") == "other"


def test_address_region():
    assert address_region("경기도 안산시 단원구 삼일로 310") == "경기"
    assert address_region("서울특별시 강남구 테헤란로 1") == "서울"
    assert address_region("") == "기타"


def test_parse_inventory_xlsx():
    data = build_inventory_xlsx(
        [
            ["pe2810", "핸드폰성지", "갤럭시 Z플립7", "SM-F971", "1,200,000", 6, "SN1", "유원"],
            ["D1474601", "직영점", "갤럭시 S25", "SM-S931N", "900000", 40, "SN2", "유원"],
        ]
    )
    parsed = parse_inventory_file("재고현황_D14746.xlsx", data)
    rows = parsed["rows"]
    assert len(rows) == 2
    # 소문자로 적혀 있어도 저장 형식으로 정규화된다
    assert rows[0]["store_code"] == "PE2810"
    assert rows[0]["holder_type"] == "partner"
    assert rows[0]["model_name"] == "SM-F971"
    assert rows[1]["holder_type"] == "retail"


def test_map_points_join_matches_store_master(server, fixtures):
    """소문자·공백이 섞인 코드로 올려도 판매점 마스터와 이어져 좌표가 붙는다."""
    from db import db_session

    code = fixtures["store_code"]
    data = build_inventory_xlsx(
        [
            [f" {code.lower()} ", "테스트판매점", "갤럭시 Z플립7", "SM-F971", "1,200,000", 6, "SN1", "테스트대리점"],
            [code, "테스트판매점", "갤럭시 Z플립7", "SM-F971", "1,200,000", 45, "SN2", "테스트대리점"],
        ]
    )
    parsed = parse_inventory_file("재고현황.xlsx", data)
    with db_session() as conn:
        dealer = dict(
            conn.execute("SELECT * FROM dealers WHERE id = ?", (fixtures["dealer_id"],)).fetchone()
        )
        summary = replace_inventory(conn, parsed, "2026-09-16T00:00:00", lambda: os.urandom(8).hex(), dealer)
        assert summary["row_count"] == 2

        result = inventory_map_points(conn, "SM-F971", dealer_id=fixtures["dealer_id"])
        assert result["total_qty"] == 2
        assert len(result["points"]) == 1  # 같은 매장으로 합쳐진다
        point = result["points"][0]
        assert point["store_code"] == code
        assert point["lat"] == pytest.approx(fixtures["lat"])
        assert point["qty"] == 2
        assert point["aged_qty"] == 1  # 보유기간 30일 이상 1대

        aged = inventory_map_points(conn, "SM-F971", dealer_id=fixtures["dealer_id"], aged_only=True)
        assert len(aged["points"]) == 1

        empty = inventory_map_points(conn, "SM-NOPE", dealer_id=fixtures["dealer_id"])
        assert empty["total_qty"] == 0


def upload_sample(fixtures):
    """판매점 1곳에 SM-F971 2대(1대는 45일 보유)를 올린다."""
    from db import db_session

    code = fixtures["store_code"]
    parsed = parse_inventory_file(
        "재고.xlsx",
        build_inventory_xlsx(
            [
                [code, "테스트판매점", "갤럭시 Z플립7", "SM-F971", "1,200,000", 6, "S1", "테스트대리점"],
                [code, "테스트판매점", "갤럭시 Z플립7", "SM-F971", "800,000", 45, "S2", "테스트대리점"],
            ]
        ),
    )
    with db_session() as conn:
        dealer = dict(
            conn.execute("SELECT * FROM dealers WHERE id = ?", (fixtures["dealer_id"],)).fetchone()
        )
        replace_inventory(conn, parsed, "2026-09-17T00:00:00", lambda: os.urandom(8).hex(), dealer)


def test_summary_catalog_breakdown_and_price_sum(server, fixtures):
    """_partner_upload_filter 를 쓰는 집계들. (한동안 함수 정의가 빠져 500 이 났었다)"""
    from db import db_session

    upload_sample(fixtures)
    dealer_id = fixtures["dealer_id"]
    with db_session() as conn:
        catalog = inventory_model_catalog(conn, dealer_id)
        shorts = [p["product_short"] for p in catalog["products"]]
        assert "갤럭시 Z플립7" in shorts

        overview = inventory_overview(conn, dealer_id)
        assert overview["total_qty"] == 2
        assert overview["aged_qty"] == 1

        breakdown = inventory_model_breakdown(conn, dealer_id=dealer_id)
        assert breakdown

        price = inventory_store_price_sum(conn, fixtures["store_code"].lower(), dealer_id)
        assert price["qty"] == 2
        assert price["total_price"] == 2_000_000

        roster = inventory_dealer_roster(conn)
        assert dealer_id in [d.get("dealer_id") for d in roster["dealers"]]


def test_roster_lists_dealers_with_staff_even_before_upload(server, fixtures):
    """임시 계정을 없앤 뒤에도, 직원이 있는 대리점은 '미업로드'로 목록에 나와야 한다."""
    from db import db_session

    with db_session() as conn:
        roster = inventory_dealer_roster(conn)
    row = next((d for d in roster["dealers"] if d["dealer_id"] == fixtures["dealer_id"]), None)
    assert row is not None
    assert row["dealer_name"] == "테스트대리점"


def test_inventory_summary_and_catalog_api(client, fixtures, admin_token):
    upload_sample(fixtures)
    # SKT 총괄: 전체 대리점 목록 포함
    H = {"X-Admin-Token": admin_token}
    summary = client.get("/api/inventory/summary", headers=H)
    assert summary.status_code == 200
    assert summary.get_json()["dealer_count"] >= 1
    assert client.get("/api/inventory/catalog", headers=H).status_code == 200

    # 대리점 직원: 자기 대리점만
    token = client.post(
        "/api/inventory/login",
        json={"username": fixtures["employee_code"], "password": fixtures["password"]},
    ).get_json()["token"]
    mine = client.get("/api/inventory/summary", headers={"X-Admin-Token": token})
    assert mine.status_code == 200
    assert mine.get_json()["total_qty"] == 2
    assert "dealers" not in mine.get_json()


def test_replace_inventory_keeps_only_latest_upload(server, fixtures):
    from db import db_session

    code = fixtures["store_code"]
    with db_session() as conn:
        dealer = dict(
            conn.execute("SELECT * FROM dealers WHERE id = ?", (fixtures["dealer_id"],)).fetchone()
        )
        first = parse_inventory_file(
            "1차.xlsx",
            build_inventory_xlsx([[code, "테스트판매점", "갤럭시", "SM-F971", "100", 1, "A", "테스트대리점"]]),
        )
        replace_inventory(conn, first, "2026-09-15T00:00:00", lambda: os.urandom(8).hex(), dealer)
        second = parse_inventory_file(
            "2차.xlsx",
            build_inventory_xlsx(
                [
                    [code, "테스트판매점", "갤럭시", "SM-F971", "100", 1, "B", "테스트대리점"],
                    [code, "테스트판매점", "갤럭시", "SM-F971", "100", 1, "C", "테스트대리점"],
                ]
            ),
        )
        replace_inventory(conn, second, "2026-09-16T00:00:00", lambda: os.urandom(8).hex(), dealer)

        uploads = conn.execute(
            "SELECT COUNT(*) AS n FROM inventory_uploads WHERE dealer_id = ?", (fixtures["dealer_id"],)
        ).fetchone()["n"]
        assert uploads == 1
        result = inventory_map_points(conn, "SM-F971", dealer_id=fixtures["dealer_id"])
        assert result["total_qty"] == 2
