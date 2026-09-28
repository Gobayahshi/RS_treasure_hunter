"""재고 엑셀 R열(색상) 파싱 + 대표상품명/모델명과 같은 드롭다운/필터 테스트."""

import os
import sys
from io import BytesIO

from openpyxl import Workbook

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from inventory import (  # noqa: E402
    inventory_map_points,
    inventory_model_catalog,
    parse_inventory_file,
    replace_inventory,
)


def build_inventory_xlsx_with_color(rows, as_of="기준일자: 2026-09-16"):
    wb = Workbook()
    ws = wb.active
    ws.append([as_of])
    ws.append(
        ["보유처매장코드", "보유처", "대표상품명", "모델명", "색상", "실구매가", "보유기간", "일련번호", "레벨0조직명"]
    )
    for row in rows:
        ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_parse_inventory_xlsx_picks_up_color_column():
    data = build_inventory_xlsx_with_color(
        [["PE2810", "핸드폰성지", "갤럭시 Z플립7", "SM-F971", "블루", "1,200,000", 6, "SN1", "유원"]]
    )
    parsed = parse_inventory_file("재고현황.xlsx", data)
    rows = parsed["rows"]
    assert len(rows) == 1
    assert rows[0]["color"] == "블루"


def test_catalog_lists_colors_per_model(server, fixtures):
    from db import db_session

    code = fixtures["store_code"]
    parsed = parse_inventory_file(
        "재고.xlsx",
        build_inventory_xlsx_with_color(
            [
                [code, "테스트판매점", "갤럭시 Z플립7", "SM-F971", "블루", "1,200,000", 6, "S1", "테스트대리점"],
                [code, "테스트판매점", "갤럭시 Z플립7", "SM-F971", "실버", "900,000", 10, "S2", "테스트대리점"],
                [code, "테스트판매점", "갤럭시 Z플립7", "SM-F971", "", "800,000", 4, "S3", "테스트대리점"],
            ]
        ),
    )
    with db_session() as conn:
        dealer = dict(
            conn.execute("SELECT * FROM dealers WHERE id = ?", (fixtures["dealer_id"],)).fetchone()
        )
        replace_inventory(conn, parsed, "2026-09-17T00:00:00", lambda: os.urandom(8).hex(), dealer)

        catalog = inventory_model_catalog(conn, fixtures["dealer_id"])
        product = next(p for p in catalog["products"] if p["product_short"] == "갤럭시 Z플립7")
        model = next(m for m in product["models"] if m["model_name"] == "SM-F971")
        # 색상이 빈 값인 행은 색상 목록에 안 나온다 (모델 전체 대수 3대에는 포함).
        assert model["qty"] == 3
        colors = {c["color"]: c["qty"] for c in model["colors"]}
        assert colors == {"블루": 1, "실버": 1}


def test_map_points_filters_by_color(server, fixtures):
    from db import db_session

    code = fixtures["store_code"]
    parsed = parse_inventory_file(
        "재고.xlsx",
        build_inventory_xlsx_with_color(
            [
                [code, "테스트판매점", "갤럭시 Z플립7", "SM-F971", "블루", "1,200,000", 6, "S1", "테스트대리점"],
                [code, "테스트판매점", "갤럭시 Z플립7", "SM-F971", "실버", "900,000", 10, "S2", "테스트대리점"],
            ]
        ),
    )
    with db_session() as conn:
        dealer = dict(
            conn.execute("SELECT * FROM dealers WHERE id = ?", (fixtures["dealer_id"],)).fetchone()
        )
        replace_inventory(conn, parsed, "2026-09-17T00:00:00", lambda: os.urandom(8).hex(), dealer)

        only_blue = inventory_map_points(
            conn, "SM-F971", dealer_id=fixtures["dealer_id"], color=["블루"]
        )
        assert only_blue["total_qty"] == 1
        assert only_blue["item_color"] == "블루"

        both = inventory_map_points(conn, "SM-F971", dealer_id=fixtures["dealer_id"])
        assert both["total_qty"] == 2
        assert both["item_color"] == ""


def test_inventory_map_api_accepts_color_param(client, fixtures, admin_token):
    from db import db_session

    code = fixtures["store_code"]
    parsed = parse_inventory_file(
        "재고.xlsx",
        build_inventory_xlsx_with_color(
            [
                [code, "테스트판매점", "갤럭시 Z플립7", "SM-F971", "블루", "1,200,000", 6, "S1", "테스트대리점"],
                [code, "테스트판매점", "갤럭시 Z플립7", "SM-F971", "실버", "900,000", 10, "S2", "테스트대리점"],
            ]
        ),
    )
    with db_session() as conn:
        dealer = dict(
            conn.execute("SELECT * FROM dealers WHERE id = ?", (fixtures["dealer_id"],)).fetchone()
        )
        replace_inventory(conn, parsed, "2026-09-17T00:00:00", lambda: os.urandom(8).hex(), dealer)

    res = client.get(
        "/api/inventory/map",
        query_string={"model": "ALL", "dealer_id": fixtures["dealer_id"], "color": "블루"},
        headers={"X-Admin-Token": admin_token},
    )
    assert res.status_code == 200
    data = res.get_json()
    assert data["total_qty"] == 1
    assert data["item_color"] == "블루"
