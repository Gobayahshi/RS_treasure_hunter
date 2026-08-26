"""재고현황 엑셀 파싱 및 판매점(P코드) 지도 집계."""

from __future__ import annotations

import re
from io import BytesIO
from typing import Any

from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet

from excel_import import cell_str, normalize_header
from confidence import haversine_distance_meters

HOLDER_CODE_ALIASES = {"보유처매장코드", "판매점코드", "매장코드"}
HOLDER_NAME_ALIASES = {"보유처", "판매점명", "매장명"}
PRODUCT_SHORT_ALIASES = {"대표상품명", "대표모델명"}
MODEL_ALIASES = {"모델명"}
PRICE_ALIASES = {"실구매가", "구매가격"}
INBOUND_ALIASES = {"입고일자"}
MOVED_ALIASES = {"재고이동출고일자"}
HOLD_DAYS_ALIASES = {"보유기간"}
SERIAL_ALIASES = {"일련번호"}
DEALER_ORG_ALIASES = {"레벨0조직명", "대리점명"}
DEALER_CODE_IN_FILENAME = re.compile(r"(D\d{5})", re.I)
AGED_DAYS = 30
HOLD_WARN_DAYS = 15
DEFAULT_MAP_MODELS = ("SM-F971", "SM-A175N", "SM-S931N")
_OVERVIEW_CACHE: dict[tuple, dict] = {}


def clear_inventory_cache() -> None:
    _OVERVIEW_CACHE.clear()


def classify_holder(store_code: str) -> str:
    code = (store_code or "").strip().upper()
    if code.startswith("P"):
        return "partner"
    if code.startswith("D") and len(code) > 6:
        return "retail"
    if code.startswith("D"):
        return "hq"
    return "other"


# 긴 접두어를 먼저 본다. 주소.xlsx 기본주소(서울특별시 …)와 시도명(서울) 둘 다 받는다.
REGION_PREFIXES = (
    ("서울", ("서울특별시", "서울시", "서울")),
    ("인천", ("인천광역시", "인천시", "인천")),
    ("경기", ("경기도", "경기")),
    ("부산", ("부산광역시", "부산시", "부산")),
    ("대구", ("대구광역시", "대구시", "대구")),
    ("광주", ("광주광역시", "광주시", "광주")),
    ("대전", ("대전광역시", "대전시", "대전")),
    ("울산", ("울산광역시", "울산시", "울산")),
    ("세종", ("세종특별자치시", "세종시", "세종")),
    ("강원", ("강원특별자치도", "강원도", "강원")),
    ("충북", ("충청북도", "충북")),
    ("충남", ("충청남도", "충남")),
    ("전북", ("전북특별자치도", "전라북도", "전북")),
    ("전남", ("전라남도", "전남")),
    ("경북", ("경상북도", "경북")),
    ("경남", ("경상남도", "경남")),
    ("제주", ("제주특별자치도", "제주도", "제주")),
)


def address_region(address: str) -> str:
    text = (address or "").strip().replace(" ", "")
    if not text:
        return "기타"
    for region, prefixes in REGION_PREFIXES:
        for prefix in prefixes:
            if text.startswith(prefix.replace(" ", "")):
                return region
    return "기타"


def _pick(row: dict[str, str], aliases: set[str]) -> str:
    for key, value in row.items():
        if key in aliases and value:
            return value
    return ""


def _find_inventory_header(ws: Worksheet) -> tuple[int, dict[str, int]]:
    for row_num, row in enumerate(ws.iter_rows(min_row=1, max_row=20, values_only=True), start=1):
        mapping = {}
        for idx, value in enumerate(row, start=1):
            key = normalize_header(value)
            if key:
                mapping[key] = idx
        if mapping.keys() & HOLDER_CODE_ALIASES and (
            mapping.keys() & PRODUCT_SHORT_ALIASES or mapping.keys() & MODEL_ALIASES
        ):
            return row_num, mapping
    return 0, {}


def _as_of_date(ws: Worksheet) -> str:
    for row in ws.iter_rows(min_row=1, max_row=5, values_only=True):
        for val in row:
            text = cell_str(val)
            match = re.search(r"일자[:\s]*([0-9]{8})", text)
            if match:
                return match.group(1)
    return ""


def is_inventory_workbook(data: bytes) -> bool:
    wb = load_workbook(BytesIO(data), data_only=True)
    try:
        for ws in wb.worksheets:
            _, headers = _find_inventory_header(ws)
            if headers:
                return True
        return False
    finally:
        wb.close()


def parse_inventory_xlsx(filename: str, data: bytes) -> dict[str, Any]:
    wb = load_workbook(BytesIO(data), data_only=True)
    try:
        rows: list[dict[str, str]] = []
        as_of = ""
        for ws in wb.worksheets:
            header_row, headers = _find_inventory_header(ws)
            if not headers:
                continue
            as_of = as_of or _as_of_date(ws)
            for row in ws.iter_rows(min_row=header_row + 1, values_only=True):
                raw = {}
                for h, col in headers.items():
                    val = row[col - 1] if col - 1 < len(row) else None
                    raw[h] = cell_str(val)
                if not any(raw.values()):
                    continue
                store_code = _pick(raw, HOLDER_CODE_ALIASES).strip().upper()
                if not store_code:
                    continue
                hold_raw = _pick(raw, HOLD_DAYS_ALIASES)
                try:
                    hold_days = str(int(float(hold_raw))) if hold_raw else ""
                except ValueError:
                    hold_days = hold_raw
                rows.append(
                    {
                        "store_code": store_code,
                        "holder_name": _pick(raw, HOLDER_NAME_ALIASES),
                        "holder_type": classify_holder(store_code),
                        "product_short": _pick(raw, PRODUCT_SHORT_ALIASES),
                        "model_name": _pick(raw, MODEL_ALIASES),
                        "purchase_price": _pick(raw, PRICE_ALIASES),
                        "inbound_date": _pick(raw, INBOUND_ALIASES)[:10],
                        "moved_date": _pick(raw, MOVED_ALIASES)[:10],
                        "hold_days": hold_days,
                        "serial": _pick(raw, SERIAL_ALIASES),
                        "dealer_org": _pick(raw, DEALER_ORG_ALIASES),
                    }
                )
        return {"filename": filename, "as_of_date": as_of, "rows": rows}
    finally:
        wb.close()


def _hold_days_int(value) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def infer_dealer(conn, filename: str, rows: list[dict[str, str]] | None = None):
    """파일명 D코드 또는 엑셀 레벨0조직명으로 대리점을 찾는다."""
    code = ""
    match = DEALER_CODE_IN_FILENAME.search(filename or "")
    if match:
        code = match.group(1).upper()
        row = conn.execute("SELECT * FROM dealers WHERE dealer_code = ?", (code,)).fetchone()
        if row:
            return dict(row)

    name_hints: list[str] = []
    if match:
        rest = (filename or "")[match.end() :]
        rest = re.split(r"[_\s.]", rest.lstrip(" _-"))[0].strip()
        if rest and rest.lower() not in {"실시간재고현황", "재고현황", "xlsx"}:
            name_hints.append(rest)
    counts: dict[str, int] = {}
    for item in rows or []:
        org = (item.get("dealer_org") or "").strip()
        if org:
            counts[org] = counts.get(org, 0) + 1
    if counts:
        name_hints.insert(0, max(counts, key=counts.get))

    dealers = [dict(r) for r in conn.execute("SELECT * FROM dealers").fetchall()]
    for hint in name_hints:
        compact = hint.replace(" ", "")
        for dealer in dealers:
            name = (dealer.get("name") or "").replace(" ", "")
            if not name:
                continue
            if name in compact or compact in name or compact.startswith(name):
                return dealer

    if code:
        name = name_hints[0] if name_hints else code
        existing = conn.execute("SELECT * FROM dealers WHERE dealer_code = ?", (code,)).fetchone()
        if existing:
            return dict(existing)
        from datetime import datetime

        dealer_id = __import__("uuid").uuid4().hex
        conn.execute(
            "INSERT INTO dealers (id, dealer_code, name, created_at) VALUES (?, ?, ?, ?)",
            (dealer_id, code, name, datetime.utcnow().isoformat()),
        )
        return dict(conn.execute("SELECT * FROM dealers WHERE id = ?", (dealer_id,)).fetchone())
    return None


def replace_inventory(conn, parsed: dict[str, Any], now_iso: str, new_id, dealer=None) -> dict:
    """해당 대리점의 이전 재고는 지우고 이번 파일만 남긴다."""
    clear_inventory_cache()
    rows = parsed.get("rows") or []
    if dealer is None:
        dealer = infer_dealer(conn, parsed.get("filename") or "", rows)
    elif not isinstance(dealer, dict):
        dealer = dict(dealer)
    if not dealer:
        raise ValueError("어느 대리점 재고인지 모릅니다. 파일명에 대리점코드(예: D14746)를 넣어주세요.")

    old = conn.execute(
        "SELECT id FROM inventory_uploads WHERE dealer_id = ?", (dealer["id"],)
    ).fetchall()
    old_ids = [r["id"] for r in old]
    if old_ids:
        placeholders = ",".join("?" * len(old_ids))
        conn.execute(f"DELETE FROM inventory_items WHERE upload_id IN ({placeholders})", old_ids)
        conn.execute(f"DELETE FROM inventory_uploads WHERE id IN ({placeholders})", old_ids)
    conn.execute("DELETE FROM inventory_items WHERE dealer_id = ?", (dealer["id"],))

    upload_id = new_id()
    conn.execute(
        """
        INSERT INTO inventory_uploads (
            id, filename, as_of_date, row_count, created_at, dealer_id, dealer_code, dealer_name
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            upload_id,
            parsed.get("filename") or "",
            parsed.get("as_of_date") or "",
            len(rows),
            now_iso,
            dealer["id"],
            dealer.get("dealer_code") or "",
            dealer.get("name") or "",
        ),
    )
    conn.executemany(
        """
        INSERT INTO inventory_items (
            id, upload_id, store_code, holder_name, holder_type,
            product_short, model_name, purchase_price, inbound_date,
            moved_date, hold_days, serial, dealer_id, dealer_code, dealer_name
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                new_id(),
                upload_id,
                row["store_code"],
                row["holder_name"],
                row["holder_type"],
                row["product_short"],
                row["model_name"],
                row["purchase_price"],
                row["inbound_date"],
                row["moved_date"],
                _hold_days_int(row["hold_days"]),
                row["serial"],
                dealer["id"],
                dealer.get("dealer_code") or "",
                dealer.get("name") or "",
            )
            for row in rows
        ],
    )
    by_type: dict[str, int] = {}
    for row in rows:
        by_type[row["holder_type"]] = by_type.get(row["holder_type"], 0) + 1
    return {
        "upload_id": upload_id,
        "filename": parsed.get("filename") or "",
        "as_of_date": parsed.get("as_of_date") or "",
        "row_count": len(rows),
        "by_holder_type": by_type,
        "dealer_id": dealer["id"],
        "dealer_code": dealer.get("dealer_code") or "",
        "dealer_name": dealer.get("name") or "",
    }


def parse_models(model_prefix: str | None) -> list[str]:
    raw = (model_prefix or "").strip().upper().replace(" ", "")
    if raw in {"ALL", "*", "ALLMODELS"}:
        return []
    if not raw:
        return list(DEFAULT_MAP_MODELS)
    parts = []
    for token in re.split(r"[,|]+", raw):
        token = token.strip()
        if not token:
            continue
        if token in {"ALL", "*"}:
            return []
        if token.startswith("SM-"):
            parts.append(token)
        elif token.startswith("SM"):
            parts.append("SM-" + token[2:])
        else:
            parts.append("SM-" + token)
    return parts


def _has_coords(lat, lng) -> bool:
    try:
        return float(lat or 0) != 0 or float(lng or 0) != 0
    except (TypeError, ValueError):
        return False


def _empty_map(
    model: str,
    include_retail: bool,
    region: str,
    keyword: str,
    dealer_id: str = "",
) -> dict:
    return {
        "model": model,
        "as_of_date": "",
        "filename": "",
        "include_retail": include_retail,
        "region": region,
        "keyword": keyword,
        "dealer_id": dealer_id,
        "total_qty": 0,
        "mapped_qty": 0,
        "unmapped_qty": 0,
        "store_count": 0,
        "points": [],
        "unmapped": [],
        "regions": [],
        "dealer_totals": [],
        "uploads": [],
        "nearest": None,
        "aged_days": AGED_DAYS,
        "shared_store_count": 0,
        "bbox": None,
        "aged_only": False,
        "area_model_totals": [],
    }


def _latest_upload_ids(conn, dealer_id: str | None = None) -> list[str]:
    if dealer_id:
        row = conn.execute(
            """
            SELECT id FROM inventory_uploads
            WHERE dealer_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (dealer_id,),
        ).fetchone()
        return [row["id"]] if row else []
    rows = conn.execute(
        """
        SELECT u.id
        FROM inventory_uploads u
        JOIN (
            SELECT COALESCE(dealer_id, id) AS grp, MAX(created_at) AS created_at
            FROM inventory_uploads
            GROUP BY COALESCE(dealer_id, id)
        ) latest
          ON COALESCE(u.dealer_id, u.id) = latest.grp
         AND u.created_at = latest.created_at
        """
    ).fetchall()
    return [r["id"] for r in rows]


def _merge_store_rows(rows) -> list[dict]:
    grouped: dict[str, dict] = {}
    for row in rows:
        code = row["store_code"]
        qty = int(row["qty"] or 0)
        item = grouped.get(code)
        if not item:
            address = row["address"] or ""
            item = {
                "store_code": code,
                "name": row["store_name"] or row["holder_name"] or code,
                "holder_name": row["holder_name"] or "",
                "address": address,
                "detail_address": row["detail_address"] or "",
                "region": address_region(address),
                "lat": row["lat"],
                "lng": row["lng"],
                "qty": 0,
                "avg_hold_days": None,
                "max_hold_days": None,
                "aged_qty": 0,
                "_dealers": {},
                "_models": {},
            }
            grouped[code] = item
        hold_sum = (item.get("_hold_sum") or 0) + (float(row["avg_hold_days"] or 0) * qty)
        item["_hold_sum"] = hold_sum
        item["qty"] += qty
        item["aged_qty"] += int(row["aged_qty"] or 0)
        max_hold = row["max_hold_days"]
        if max_hold is not None:
            prev = item["max_hold_days"]
            item["max_hold_days"] = int(max_hold) if prev is None else max(int(prev), int(max_hold))
        dealer_key = row["dealer_id"] or row["dealer_code"] or row["dealer_name"] or "unknown"
        dealer = item["_dealers"].get(dealer_key)
        if not dealer:
            dealer = {
                "dealer_id": row["dealer_id"] or "",
                "dealer_code": row["dealer_code"] or "",
                "dealer_name": row["dealer_name"] or "미지정",
                "qty": 0,
                "avg_hold_days": None,
                "max_hold_days": None,
                "aged_qty": 0,
            }
            item["_dealers"][dealer_key] = dealer
        dealer["qty"] += qty
        dealer["aged_qty"] += int(row["aged_qty"] or 0)
        if row["max_hold_days"] is not None:
            prev = dealer["max_hold_days"]
            dealer["max_hold_days"] = (
                int(row["max_hold_days"]) if prev is None else max(int(prev), int(row["max_hold_days"]))
            )
        model_key = (row["model_key"] if "model_key" in row.keys() else "") or ""
        if model_key:
            item["_models"][model_key] = item["_models"].get(model_key, 0) + qty
        if row["holder_name"]:
            item["holder_name"] = row["holder_name"]
        if row["store_name"]:
            item["name"] = row["store_name"]
    out = []
    for item in grouped.values():
        hold_sum = item.pop("_hold_sum", 0)
        dealers_map = item.pop("_dealers")
        models_map = item.pop("_models")
        if item["qty"]:
            item["avg_hold_days"] = round(hold_sum / item["qty"], 1)
        item["dealers"] = sorted(dealers_map.values(), key=lambda d: (-d["qty"], d["dealer_name"]))
        item["models"] = [
            {"model": name, "qty": qty}
            for name, qty in sorted(models_map.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        item["shared"] = len(item["dealers"]) > 1
        out.append(item)
    out.sort(key=lambda p: (-p["qty"], p["store_code"]))
    return out


def normalize_bbox(bbox) -> tuple[float, float, float, float] | None:
    if not bbox:
        return None
    if isinstance(bbox, dict):
        try:
            south = float(bbox.get("south"))
            west = float(bbox.get("west"))
            north = float(bbox.get("north"))
            east = float(bbox.get("east"))
        except (TypeError, ValueError):
            return None
    elif isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        try:
            south, west, north, east = (float(x) for x in bbox)
        except (TypeError, ValueError):
            return None
    else:
        return None
    if south > north:
        south, north = north, south
    if west > east:
        west, east = east, west
    if south == north or west == east:
        return None
    return south, west, north, east


def _in_bbox(lat, lng, bbox: tuple[float, float, float, float]) -> bool:
    south, west, north, east = bbox
    try:
        y = float(lat)
        x = float(lng)
    except (TypeError, ValueError):
        return False
    return south <= y <= north and west <= x <= east


def inventory_map_points(
    conn,
    model_prefix: str,
    include_retail: bool = False,
    region: str | None = None,
    lat: float | None = None,
    lng: float | None = None,
    keyword: str | None = None,
    dealer_id: str | None = None,
    bbox=None,
    aged_only: bool = False,
    radius_km: float | None = None,
) -> dict:
    models = parse_models(model_prefix)
    model = ",".join(models) if models else "all"
    holders = ("partner", "retail") if include_retail else ("partner",)
    placeholders = ",".join("?" * len(holders))
    wanted_region = (region or "").strip()
    wanted_keyword = (keyword or "").strip()
    wanted_dealer = (dealer_id or "").strip()
    wanted_bbox = normalize_bbox(bbox)
    try:
        radius_m = float(radius_km) * 1000 if radius_km not in (None, "") else None
    except (TypeError, ValueError):
        radius_m = None
    if radius_m is not None and radius_m <= 0:
        radius_m = None

    upload_ids = _latest_upload_ids(conn, wanted_dealer or None)
    if not upload_ids:
        return _empty_map(model, include_retail, wanted_region, wanted_keyword, wanted_dealer)

    up_ph = ",".join("?" * len(upload_ids))
    model_wheres = []
    model_params: list = []
    case_sql = []
    case_params: list = []
    if models:
        for name in models:
            like = f"{name}%"
            model_wheres.append(
                "(UPPER(COALESCE(i.product_short, '')) LIKE ? OR UPPER(COALESCE(i.model_name, '')) LIKE ?)"
            )
            model_params.extend([like, like])
            case_sql.append(
                "WHEN UPPER(COALESCE(i.product_short, '')) LIKE ? OR UPPER(COALESCE(i.model_name, '')) LIKE ? THEN ?"
            )
            case_params.extend([like, like, name])
        model_key_expr = "CASE " + " ".join(case_sql) + " ELSE UPPER(COALESCE(i.product_short, i.model_name, '')) END"
        model_filter_sql = "AND (" + " OR ".join(model_wheres) + ")"
    else:
        model_key_expr = "UPPER(COALESCE(NULLIF(TRIM(i.product_short), ''), TRIM(i.model_name), ''))"
        model_filter_sql = ""
    rows = conn.execute(
        f"""
        SELECT
            i.store_code,
            i.dealer_id,
            MAX(i.dealer_code) AS dealer_code,
            MAX(i.dealer_name) AS dealer_name,
            MAX(i.holder_name) AS holder_name,
            {model_key_expr} AS model_key,
            COUNT(*) AS qty,
            AVG(i.hold_days) AS avg_hold_days,
            MAX(i.hold_days) AS max_hold_days,
            SUM(CASE WHEN i.hold_days >= ? THEN 1 ELSE 0 END) AS aged_qty,
            s.name AS store_name,
            s.address,
            s.detail_address,
            s.lat,
            s.lng
        FROM inventory_items i
        LEFT JOIN stores s ON UPPER(TRIM(COALESCE(s.store_code, ''))) = UPPER(TRIM(COALESCE(i.store_code, '')))
        WHERE i.upload_id IN ({up_ph})
          AND i.holder_type IN ({placeholders})
          {model_filter_sql}
        GROUP BY i.store_code, i.dealer_id, model_key
        """,
        (*case_params, AGED_DAYS, *upload_ids, *holders, *model_params),
    ).fetchall()

    merged = _merge_store_rows(rows)
    points = []
    unmapped = []
    for item in merged:
        if _has_coords(item["lat"], item["lng"]):
            points.append(item)
        else:
            unmapped.append(item)

    region_counts: dict[str, dict[str, int]] = {}
    for item in points:
        bucket = region_counts.setdefault(item["region"], {"qty": 0, "stores": 0})
        bucket["qty"] += item["qty"]
        bucket["stores"] += 1
    regions = [
        {"region": name, "qty": stats["qty"], "stores": stats["stores"]}
        for name, stats in sorted(region_counts.items(), key=lambda kv: (-kv[1]["qty"], kv[0]))
    ]

    if wanted_region:
        points = [p for p in points if p["region"] == wanted_region]
        unmapped = [u for u in unmapped if u["region"] == wanted_region]
    if wanted_keyword:
        key = wanted_keyword.replace(" ", "").lower()

        def _hit(item):
            dealer_blob = "".join(d.get("dealer_name") or "" for d in item.get("dealers") or [])
            blob = f"{item.get('name','')}{item.get('holder_name','')}{item.get('address','')}{item.get('store_code','')}{dealer_blob}"
            return key in blob.replace(" ", "").lower()

        points = [p for p in points if _hit(p)]
        unmapped = [u for u in unmapped if _hit(u)]

    if wanted_bbox:
        points = [p for p in points if _in_bbox(p.get("lat"), p.get("lng"), wanted_bbox)]
        unmapped = []
    if aged_only:
        points = [p for p in points if (p.get("aged_qty") or 0) > 0]
        unmapped = [u for u in unmapped if (u.get("aged_qty") or 0) > 0]

    nearest = None
    if lat is not None and lng is not None:
        for item in points:
            item["distance_meters"] = round(
                haversine_distance_meters(lat, lng, float(item["lat"]), float(item["lng"]))
            )
        points.sort(key=lambda p: (p.get("distance_meters") if p.get("distance_meters") is not None else 10**12, -p["qty"]))
        if radius_m is not None:
            nearby = [p for p in points if (p.get("distance_meters") or 10**12) <= radius_m]
            if nearby:
                points = nearby
        if points:
            nearest = dict(points[0])

    mapped_qty = sum(p["qty"] for p in points)
    unmapped_qty = sum(u["qty"] for u in unmapped)
    dealer_totals: dict[str, dict] = {}
    for item in points + unmapped:
        for dealer in item.get("dealers") or []:
            key = dealer.get("dealer_id") or dealer.get("dealer_code") or dealer.get("dealer_name")
            bucket = dealer_totals.setdefault(
                key,
                {
                    "dealer_id": dealer.get("dealer_id") or "",
                    "dealer_code": dealer.get("dealer_code") or "",
                    "dealer_name": dealer.get("dealer_name") or "",
                    "qty": 0,
                    "stores": 0,
                },
            )
            bucket["qty"] += dealer["qty"]
            bucket["stores"] += 1
    uploads = [
        dict(r)
        for r in conn.execute(
            f"SELECT filename, as_of_date, row_count, dealer_code, dealer_name FROM inventory_uploads WHERE id IN ({up_ph})",
            upload_ids,
        ).fetchall()
    ]
    as_of_dates = sorted({u.get("as_of_date") or "" for u in uploads if u.get("as_of_date")})
    shared_store_count = sum(1 for p in points if p.get("shared"))
    model_totals: dict[str, dict] = {name: {"model": name, "qty": 0, "stores": 0} for name in models}
    for item in points + unmapped:
        seen = set()
        for m in item.get("models") or []:
            bucket = model_totals.setdefault(m["model"], {"model": m["model"], "qty": 0, "stores": 0})
            bucket["qty"] += m["qty"]
            if m["model"] not in seen:
                bucket["stores"] += 1
                seen.add(m["model"])

    return {
        "model": model,
        "models": models,
        "model_totals": [model_totals[name] for name in models if name in model_totals],
        "as_of_date": ",".join(as_of_dates),
        "filename": ", ".join(u.get("filename") or "" for u in uploads),
        "include_retail": include_retail,
        "region": wanted_region,
        "keyword": wanted_keyword,
        "dealer_id": wanted_dealer,
        "total_qty": mapped_qty + unmapped_qty,
        "store_count": len(points) + len(unmapped),
        "mapped_qty": mapped_qty,
        "unmapped_qty": unmapped_qty,
        "points": points,
        "unmapped": unmapped,
        "regions": regions,
        "dealer_totals": sorted(dealer_totals.values(), key=lambda d: (-d["qty"], d["dealer_name"])),
        "uploads": uploads,
        "nearest": nearest,
        "aged_days": AGED_DAYS,
        "shared_store_count": shared_store_count,
        "bbox": (
            {
                "south": wanted_bbox[0],
                "west": wanted_bbox[1],
                "north": wanted_bbox[2],
                "east": wanted_bbox[3],
            }
            if wanted_bbox
            else None
        ),
        "aged_only": bool(aged_only),
        "radius_km": (radius_m / 1000) if radius_m else None,
        "area_model_totals": sorted(
            model_totals.values(), key=lambda m: (-m["qty"], m["model"])
        ),
    }


def _partner_upload_filter(conn, dealer_id: str | None = None) -> tuple[list[str], list]:
    upload_ids = _latest_upload_ids(conn, dealer_id or None)
    if not upload_ids:
        return [], []
    uploads = [
        dict(r)
        for r in conn.execute(
            f"SELECT filename, as_of_date, row_count, dealer_code, dealer_name FROM inventory_uploads WHERE id IN ({','.join('?' * len(upload_ids))})",
            upload_ids,
        ).fetchall()
    ]
    return upload_ids, uploads


def _region_where(region: str) -> tuple[str, list]:
    wanted = (region or "").strip()
    if not wanted:
        return "", []
    prefixes = []
    for name, opts in REGION_PREFIXES:
        if name == wanted:
            prefixes = list(opts)
            break
    if not prefixes:
        return "", []
    clauses = []
    params: list = []
    for prefix in prefixes:
        clauses.append("REPLACE(COALESCE(s.address, ''), ' ', '') LIKE ?")
        params.append(prefix.replace(" ", "") + "%")
    return "(" + " OR ".join(clauses) + ")", params


def _scope_filters(region: str | None, keyword: str | None, bbox) -> tuple[str, list]:
    sql = ""
    params: list = []
    region_sql, region_params = _region_where(region or "")
    if region_sql:
        sql += f" AND {region_sql}"
        params.extend(region_params)
    wanted_bbox = normalize_bbox(bbox)
    if wanted_bbox:
        south, west, north, east = wanted_bbox
        sql += " AND s.lat IS NOT NULL AND s.lng IS NOT NULL AND s.lat BETWEEN ? AND ? AND s.lng BETWEEN ? AND ?"
        params.extend([south, north, west, east])
    key = (keyword or "").strip()
    if key:
        like = f"%{key}%"
        sql += """ AND (
            COALESCE(s.name, '') LIKE ?
            OR COALESCE(s.address, '') LIKE ?
            OR COALESCE(i.holder_name, '') LIKE ?
            OR COALESCE(i.store_code, '') LIKE ?
            OR COALESCE(i.dealer_name, '') LIKE ?
        )"""
        params.extend([like, like, like, like, like])
    return sql, params


def inventory_model_breakdown(
    conn,
    dealer_id: str | None = None,
    region: str | None = None,
    keyword: str | None = None,
    bbox=None,
    limit: int = 20,
) -> list[dict]:
    """필터에 맞는 모든 기종 대수. 지도에 안 올린 기종도 포함한다."""
    upload_ids, _uploads = _partner_upload_filter(conn, dealer_id)
    if not upload_ids:
        return []
    extra, extra_params = _scope_filters(region, keyword, bbox)
    rows = conn.execute(
        f"""
        SELECT
            COALESCE(NULLIF(i.product_short, ''), i.model_name, '미상') AS model,
            COUNT(*) AS qty,
            COUNT(DISTINCT i.store_code) AS stores,
            SUM(CASE WHEN i.hold_days >= ? THEN 1 ELSE 0 END) AS aged_qty
        FROM inventory_items i
        LEFT JOIN stores s ON s.store_code = i.store_code
        WHERE i.upload_id IN ({','.join('?' * len(upload_ids))})
          AND i.holder_type = 'partner'
          {extra}
        GROUP BY COALESCE(NULLIF(i.product_short, ''), i.model_name, '미상')
        ORDER BY qty DESC
        LIMIT ?
        """,
        (AGED_DAYS, *upload_ids, *extra_params, limit),
    ).fetchall()
    return [
        {
            "model": r["model"],
            "qty": int(r["qty"] or 0),
            "stores": int(r["stores"] or 0),
            "aged_qty": int(r["aged_qty"] or 0),
        }
        for r in rows
    ]


def inventory_overview(conn, dealer_id: str | None = None) -> dict:
    """Gemini에 넘길 전체 재고 요약. SQL 집계만 사용한다."""
    upload_ids, uploads = _partner_upload_filter(conn, dealer_id)
    cache_key = (dealer_id or "", tuple(upload_ids))
    cached = _OVERVIEW_CACHE.get(cache_key)
    if cached is not None:
        return cached
    empty = {
        "as_of_date": "",
        "uploads": uploads,
        "total_qty": 0,
        "store_count": 0,
        "by_dealer": [],
        "by_region": [],
        "by_model": [],
        "hold_buckets": {"under_15": 0, "days_15_29": 0, "days_30_plus": 0},
        "aged_qty": 0,
        "top_aged_stores": [],
        "top_stores": [],
    }
    if not upload_ids:
        return empty
    up_ph = ",".join("?" * len(upload_ids))
    base = f"""
        FROM inventory_items i
        LEFT JOIN stores s ON s.store_code = i.store_code
        WHERE i.upload_id IN ({up_ph})
          AND i.holder_type = 'partner'
    """
    totals = conn.execute(
        f"""
        SELECT COUNT(*) AS qty,
               COUNT(DISTINCT i.store_code) AS stores,
               SUM(CASE WHEN COALESCE(i.hold_days, 0) < 15 THEN 1 ELSE 0 END) AS fresh_qty,
               SUM(CASE WHEN i.hold_days >= 15 AND i.hold_days < 30 THEN 1 ELSE 0 END) AS warn_qty,
               SUM(CASE WHEN i.hold_days >= ? THEN 1 ELSE 0 END) AS aged_qty
        {base}
        """,
        (AGED_DAYS, *upload_ids),
    ).fetchone()
    by_dealer = [
        dict(r)
        for r in conn.execute(
            f"""
            SELECT i.dealer_id, MAX(i.dealer_code) AS dealer_code, MAX(i.dealer_name) AS dealer_name,
                   COUNT(*) AS qty, COUNT(DISTINCT i.store_code) AS stores,
                   SUM(CASE WHEN i.hold_days >= ? THEN 1 ELSE 0 END) AS aged_qty
            {base}
            GROUP BY i.dealer_id
            ORDER BY qty DESC
            """,
            (AGED_DAYS, *upload_ids),
        ).fetchall()
    ]
    by_model = [
        dict(r)
        for r in conn.execute(
            f"""
            SELECT COALESCE(NULLIF(i.product_short, ''), i.model_name, '미상') AS model,
                   COUNT(*) AS qty, COUNT(DISTINCT i.store_code) AS stores,
                   SUM(CASE WHEN i.hold_days >= ? THEN 1 ELSE 0 END) AS aged_qty
            {base}
            GROUP BY COALESCE(NULLIF(i.product_short, ''), i.model_name, '미상')
            ORDER BY qty DESC
            LIMIT 12
            """,
            (AGED_DAYS, *upload_ids),
        ).fetchall()
    ]
    region_cases = []
    region_params: list = []
    for name, prefixes in REGION_PREFIXES:
        parts = []
        for prefix in prefixes:
            parts.append("REPLACE(COALESCE(s.address, ''), ' ', '') LIKE ?")
            region_params.append(prefix.replace(" ", "") + "%")
        region_cases.append(f"WHEN {' OR '.join(parts)} THEN ?")
        region_params.append(name)
    region_expr = "CASE " + " ".join(region_cases) + " ELSE '기타' END"
    by_region = [
        dict(r)
        for r in conn.execute(
            f"""
            SELECT {region_expr} AS region,
                   COUNT(*) AS qty, COUNT(DISTINCT i.store_code) AS stores,
                   SUM(CASE WHEN i.hold_days >= ? THEN 1 ELSE 0 END) AS aged_qty
            {base}
            GROUP BY region
            ORDER BY qty DESC
            """,
            (*region_params, AGED_DAYS, *upload_ids),
        ).fetchall()
    ]
    top_aged = [
        dict(r)
        for r in conn.execute(
            f"""
            SELECT i.store_code,
                   COALESCE(s.name, MAX(i.holder_name), i.store_code) AS name,
                   s.address,
                   MAX(i.dealer_name) AS dealer_name,
                   COUNT(*) AS qty,
                   SUM(CASE WHEN i.hold_days >= ? THEN 1 ELSE 0 END) AS aged_qty,
                   MAX(i.hold_days) AS max_hold_days
            {base}
            GROUP BY i.store_code
            HAVING SUM(CASE WHEN i.hold_days >= ? THEN 1 ELSE 0 END) > 0
            ORDER BY aged_qty DESC, max_hold_days DESC
            LIMIT 8
            """,
            (AGED_DAYS, *upload_ids, AGED_DAYS),
        ).fetchall()
    ]
    top_stores = [
        dict(r)
        for r in conn.execute(
            f"""
            SELECT i.store_code,
                   COALESCE(s.name, MAX(i.holder_name), i.store_code) AS name,
                   s.address,
                   COUNT(*) AS qty,
                   SUM(CASE WHEN i.hold_days >= ? THEN 1 ELSE 0 END) AS aged_qty
            {base}
            GROUP BY i.store_code
            ORDER BY qty DESC
            LIMIT 8
            """,
            (AGED_DAYS, *upload_ids),
        ).fetchall()
    ]
    as_of_dates = sorted({u.get("as_of_date") or "" for u in uploads if u.get("as_of_date")})
    aged_qty = int(totals["aged_qty"] or 0)
    result = {
        "as_of_date": ",".join(as_of_dates),
        "uploads": uploads,
        "total_qty": int(totals["qty"] or 0),
        "store_count": int(totals["stores"] or 0),
        "by_dealer": [
            {
                "dealer_id": d["dealer_id"] or "",
                "dealer_code": d["dealer_code"] or "",
                "dealer_name": d["dealer_name"] or "미지정",
                "qty": int(d["qty"] or 0),
                "stores": int(d["stores"] or 0),
                "aged_qty": int(d["aged_qty"] or 0),
            }
            for d in by_dealer
        ],
        "by_region": [
            {
                "region": r["region"],
                "qty": int(r["qty"] or 0),
                "stores": int(r["stores"] or 0),
                "aged_qty": int(r["aged_qty"] or 0),
            }
            for r in by_region
        ],
        "by_model": [
            {
                "model": m["model"],
                "qty": int(m["qty"] or 0),
                "stores": int(m["stores"] or 0),
                "aged_qty": int(m["aged_qty"] or 0),
            }
            for m in by_model
        ],
        "hold_buckets": {
            "under_15": int(totals["fresh_qty"] or 0),
            "days_15_29": int(totals["warn_qty"] or 0),
            "days_30_plus": aged_qty,
        },
        "aged_qty": aged_qty,
        "top_aged_stores": [
            {
                "store_code": r["store_code"],
                "name": r["name"],
                "address": r["address"] or "",
                "dealer_name": r["dealer_name"] or "",
                "aged_qty": int(r["aged_qty"] or 0),
                "qty": int(r["qty"] or 0),
                "max_hold_days": int(r["max_hold_days"] or 0),
            }
            for r in top_aged
        ],
        "top_stores": [
            {
                "store_code": r["store_code"],
                "name": r["name"],
                "address": r["address"] or "",
                "qty": int(r["qty"] or 0),
                "aged_qty": int(r["aged_qty"] or 0),
            }
            for r in top_stores
        ],
    }
    _OVERVIEW_CACHE[cache_key] = result
    return result
