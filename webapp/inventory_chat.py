"""재고 질문(챗봇) 의도 파악과 답변 생성.

1단계: 텍스트 질문 → 텍스트 답.
음성 입력/출력은 같은 ask_inventory() 결과를 읽기만 하면 되도록
answer / speech 를 함께 내려준다.
"""

from __future__ import annotations

import re

from inventory import (
    REGION_PREFIXES,
    inventory_map_points,
    inventory_model_breakdown,
    inventory_overview,
    inventory_store_price_sum,
    normalize_bbox,
)
from inventory_llm import interpret_inventory_question, llm_available

DEFAULT_MODEL = ""

NEAREST_HINTS = (
    "가까운",
    "근처",
    "내 위치",
    "내위치",
    "현재 위치",
    "지금 있는",
    "여기",
    "주변",
    "가장 가깝",
)
HELP_HINTS = ("도움", "뭐 물어", "어떻게", "예시", "help")
TOTAL_HINTS = ("전체", "총", "다 합", "합계")
PRICE_HINTS = ("가격", "금액", "실구매가", "실구매")
AGED_HINTS = ("오래", "묵은", "체화", "30일", "장기보유", "장기 보유", "보유기간", "출고된지")
COMPARE_HINTS = ("비교", "어디가 더", "더 많", "차이")
AREA_HINTS = ("이 영역", "이영역", "선택한 영역", "고른 영역", "지도에서 선택", "박스", "사각형")
ANALYZE_HINTS = ("어때", "현황", "요약", "추천", "먼저", "문제", "분석", "어디부터")
COLOR_HINTS = ("색으로", "색깔", "색상", "칠해", "표시해", "보여줘", "보여 줘")

_PIN_COLORS = (
    ("빨간색", "#dc2626"),
    ("빨강색", "#dc2626"),
    ("빨간", "#dc2626"),
    ("레드", "#dc2626"),
    ("red", "#dc2626"),
    ("주황색", "#ea580c"),
    ("주황", "#ea580c"),
    ("오렌지", "#ea580c"),
    ("노란색", "#ca8a04"),
    ("노랑색", "#ca8a04"),
    ("노란", "#ca8a04"),
    ("초록색", "#16a34a"),
    ("초록", "#16a34a"),
    ("녹색", "#16a34a"),
    ("그린", "#16a34a"),
    ("파란색", "#2563eb"),
    ("파랑색", "#2563eb"),
    ("파란", "#2563eb"),
    ("블루", "#2563eb"),
    ("보라색", "#7c3aed"),
    ("보라", "#7c3aed"),
    ("핑크색", "#db2777"),
    ("분홍색", "#db2777"),
    ("핑크", "#db2777"),
    ("분홍", "#db2777"),
)


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").strip().lower())


def _extract_model(text: str) -> str:
    raw = (text or "").upper().replace(" ", "")
    match = re.search(r"SM-?[A-Z0-9]{3,}", raw)
    if match:
        token = match.group(0)
        if not token.startswith("SM-"):
            token = "SM-" + token[2:]
        return token
    match = re.search(r"F\d{3,4}N?", raw)
    if match:
        return f"SM-{match.group(0)}"
    match = re.search(r"[AS]\d{3,4}N?", raw)
    if match:
        return f"SM-{match.group(0)}"
    return ""


def _extract_pin_color(text: str) -> str:
    compact = re.sub(r"\s+", "", text or "").lower()
    ranked: list[tuple[int, str]] = []
    for word, color in _PIN_COLORS:
        key = word.lower()
        if key and key in compact:
            ranked.append((len(key), color))
    if not ranked:
        return ""
    ranked.sort(reverse=True)
    return ranked[0][1]


def _extract_region(text: str) -> str:
    compact = _compact(text)
    # 긴 이름 우선
    ranked: list[tuple[int, str]] = []
    for region, prefixes in REGION_PREFIXES:
        for prefix in prefixes:
            p = prefix.replace(" ", "").lower()
            if p and p in compact:
                ranked.append((len(p), region))
    if not ranked:
        return ""
    ranked.sort(reverse=True)
    return ranked[0][1]


def _extract_store_code(text: str) -> str:
    match = re.search(r"P\s*\d{4,}", text or "", re.I)
    if not match:
        return ""
    return re.sub(r"\s+", "", match.group(0)).upper()


def _extract_keyword(text: str, region: str, extra_drop: list[str] | None = None) -> str:
    """지역명·기종·조사만 남기고, 지명/매장명 후보를 고른다."""
    cleaned = (text or "").strip()
    cleaned = re.sub(r"SM-?[A-Za-z0-9\-]+", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"F\d{3,4}", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"[AS]\d{3,4}N?", " ", cleaned, flags=re.I)
    drop = [
        region,
        "서울특별시",
        "경기도",
        "인천광역시",
        "현재",
        "지금",
        "재고",
        "전체",
        "총",
        "합계",
        "다합",
        "숫자",
        "몇 대",
        "몇대",
        "몇개",
        "몇 개",
        "대인지",
        "알려",
        "확인",
        "보여",
        "찾아",
        "위치",
        "기준",
        "가장",
        "가까운",
        "근처",
        "있는",
        "곳의",
        "곳",
        "판매점",
        "기종",
        "모델",
        "질문",
        "거야",
        "인가요",
        "있어요",
        "있어",
        "해줘",
        "해 줘",
        "좀",
        "을",
        "를",
        "에",
        "의",
        "은",
        "는",
        "이",
        "가",
        "으로",
        "로",
        "에서",
        "하고",
        "랑",
    ]
    for word in extra_drop or []:
        if word:
            drop.append(word)
    for word in drop:
        if word:
            cleaned = cleaned.replace(word, " ")
    cleaned = re.sub(r"[^0-9A-Za-z가-힣\s]", " ", cleaned)
    tokens = [t for t in cleaned.split() if len(t) >= 2]
    if not tokens:
        return ""
    tokens.sort(key=len, reverse=True)
    return tokens[0]


def _format_km(meters: int | None) -> str:
    if meters is None:
        return ""
    if meters < 1000:
        return f"{meters}m"
    return f"{meters / 1000:.1f}km"


def _as_of(data: dict) -> str:
    bits = []
    for upload in data.get("uploads") or []:
        day = upload.get("as_of_date") or ""
        if len(day) == 8:
            day = f"{day[:4]}-{day[4:6]}-{day[6:]}"
        name = upload.get("dealer_name") or ""
        if name and day:
            bits.append(f"{name} {day}")
        elif day:
            bits.append(day)
    if bits:
        return ", ".join(dict.fromkeys(bits))
    day = data.get("as_of_date") or ""
    if "," in day:
        return ", ".join(
            f"{part[:4]}-{part[4:6]}-{part[6:]}" if len(part) == 8 else part
            for part in day.split(",")
            if part
        )
    if len(day) == 8:
        return f"{day[:4]}-{day[4:6]}-{day[6:]}"
    return day


def _dealer_bit(data: dict) -> str:
    totals = data.get("dealer_totals") or []
    if len(totals) < 2:
        return ""
    return " " + ", ".join(f"{t['dealer_name']} {t['qty']}대" for t in totals) + "."


def _hold_bit(point: dict | None) -> str:
    if not point:
        return ""
    aged = point.get("aged_qty") or 0
    mx = point.get("max_hold_days")
    if aged:
        return f" 이 중 {aged}대가 30일 이상입니다."
    if mx is not None:
        return f" 최장 보유 {mx}일입니다."
    return ""


def _extract_dealer(text: str, dealers: list[dict]) -> dict | None:
    compact = _compact(text)
    ranked: list[tuple[int, dict]] = []
    for dealer in dealers:
        name = _compact(dealer.get("name") or "")
        code = (dealer.get("dealer_code") or "").lower()
        if name and name in compact:
            ranked.append((len(name), dealer))
        if code and code in compact:
            ranked.append((len(code) + 5, dealer))
    if not ranked:
        return None
    ranked.sort(reverse=True)
    return ranked[0][1]


def _top_stores_bit(points: list, n: int = 3) -> str:
    if not points:
        return ""
    ranked = sorted(points, key=lambda p: p.get("qty") or 0, reverse=True)[:n]
    bits = [f"{p['name']} {p['qty']}대" for p in ranked]
    return " 많은 곳부터 " + ", ".join(bits) + "입니다."


def parse_inventory_question(text: str, dealers: list[dict] | None = None) -> dict:
    raw = (text or "").strip()
    compact = _compact(raw)
    model = _extract_model(raw)
    region = _extract_region(raw)
    dealer = _extract_dealer(raw, dealers or [])
    store_code = _extract_store_code(raw)
    extra = []
    if dealer:
        extra.extend([dealer.get("name") or "", dealer.get("dealer_code") or "", "대리점"])
    keyword = _extract_keyword(raw, region, extra)
    if store_code:
        keyword = store_code

    has_price = any(h in compact for h in PRICE_HINTS) or (
        store_code and "합" in compact and "얼마" in compact
    )
    if any(h in compact for h in HELP_HINTS) or not compact:
        intent = "help"
    elif store_code and has_price:
        intent = "price"
    elif any(h in compact for h in AREA_HINTS):
        intent = "bbox"
    elif any(h in compact for h in NEAREST_HINTS):
        intent = "nearest"
    elif any(h in compact for h in AGED_HINTS):
        intent = "aged"
    elif any(h in compact for h in COMPARE_HINTS):
        intent = "compare"
    elif any(h in compact for h in ANALYZE_HINTS):
        intent = "analyze"
    elif region:
        intent = "region"
    elif dealer and (any(h in compact for h in TOTAL_HINTS) or not keyword):
        intent = "total"
    elif any(h in compact for h in TOTAL_HINTS) or not keyword:
        intent = "total"
    else:
        intent = "keyword"

    return {
        "intent": intent,
        "model": model or "ALL",
        "models": [model] if model else [],
        "region": region if intent in {"region", "nearest", "analyze", "aged", "compare", "bbox"} else "",
        "keyword": keyword if intent in {"keyword", "nearest", "analyze", "aged", "compare", "bbox", "price"} else "",
        "store_code": store_code,
        "dealer_id": dealer["id"] if dealer else "",
        "dealer_name": dealer.get("name") or "" if dealer else "",
        "raw": raw,
        "needs_location": intent == "nearest",
        "use_map_area": intent == "bbox",
        "aged_only": intent == "aged",
        "pin_color": _extract_pin_color(raw),
        "nlu": "rules",
    }


def ask_inventory(
    conn,
    text: str,
    lat: float | None = None,
    lng: float | None = None,
    bbox=None,
    dealer_id: str | None = None,
) -> dict:
    all_dealers = [dict(r) for r in conn.execute("SELECT id, dealer_code, name FROM dealers").fetchall()]
    dealers = [d for d in all_dealers if d["id"] == dealer_id] if dealer_id else all_dealers
    nlu = "rules"
    rules = parse_inventory_question(text, dealers)
    parsed = None
    # 30일/체화처럼 규칙이 이미 확실한 질문은 LLM을 건너뛴다. Render 30초 제한에 걸린다.
    skip_llm = rules.get("intent") in {"aged", "price"} or bool(rules.get("store_code"))
    if llm_available() and not skip_llm:
        parsed = interpret_inventory_question(text, dealers)
        if parsed:
            nlu = "llm"
            if not (parsed.get("keyword") or "").strip() and (rules.get("keyword") or "").strip():
                parsed["keyword"] = rules["keyword"]
                if parsed.get("intent") in {"total", "analyze", "help"}:
                    parsed["intent"] = "keyword"
            if (not parsed.get("models")) and rules.get("models"):
                parsed["models"] = rules["models"]
                if not parsed.get("model") or str(parsed.get("model")).upper() in {"", "ALL", "*"}:
                    parsed["model"] = rules.get("model") or "ALL"
    if not parsed:
        parsed = rules
    color = _extract_pin_color(text)
    if color:
        parsed["pin_color"] = color
        compact = _compact(text)
        if any(h in compact for h in AGED_HINTS) or any(h in compact for h in COLOR_HINTS):
            if any(h in compact for h in AGED_HINTS):
                parsed["aged_only"] = True
    parsed["nlu"] = nlu
    if dealer_id:
        scoped = next((d for d in all_dealers if d["id"] == dealer_id), None)
        parsed["dealer_id"] = dealer_id
        parsed["dealer_name"] = (scoped or {}).get("name") or parsed.get("dealer_name") or ""
        if parsed.get("intent") == "compare":
            parsed["intent"] = "analyze"
    result = _answer_from_parsed(conn, parsed, lat, lng, bbox=bbox)
    result["nlu"] = nlu
    return result


def _llm_facts(parsed: dict, result: dict) -> dict:
    data = result.get("map") or {}
    nearest = data.get("nearest")
    points = data.get("points") or []
    overview = result.get("overview") or {}
    aged_qty = sum(int(p.get("aged_qty") or 0) for p in points)
    return {
        "intent": parsed.get("intent"),
        "models_on_map": data.get("models") or [parsed.get("model")],
        "dealer_filter": parsed.get("dealer_name") or "",
        "region_filter": parsed.get("region") or data.get("region") or "",
        "keyword_filter": parsed.get("keyword") or "",
        "bbox": data.get("bbox"),
        "aged_only": bool(data.get("aged_only")),
        "scope_qty": data.get("mapped_qty"),
        "scope_stores": len(points),
        "scope_aged_qty": aged_qty,
        "dealer_totals": data.get("dealer_totals") or [],
        "model_totals_on_map": data.get("model_totals") or [],
        "all_models_in_scope": (data.get("area_model_totals") or [])[:15],
        "regions": (data.get("regions") or [])[:12],
        "top_stores": [
            {
                "name": p.get("name"),
                "code": p.get("store_code"),
                "qty": p.get("qty"),
                "aged_qty": p.get("aged_qty") or 0,
                "max_hold_days": p.get("max_hold_days"),
                "dealers": [d.get("dealer_name") for d in (p.get("dealers") or [])],
                "address": p.get("address") or "",
            }
            for p in points[:12]
        ],
        "nearest": None
        if not nearest
        else {
            "name": nearest.get("name"),
            "code": nearest.get("store_code"),
            "qty": nearest.get("qty"),
            "aged_qty": nearest.get("aged_qty") or 0,
            "distance_meters": nearest.get("distance_meters"),
            "address": nearest.get("address"),
        },
        "overview": {
            "total_qty": overview.get("total_qty"),
            "store_count": overview.get("store_count"),
            "by_dealer": overview.get("by_dealer") or [],
            "by_region": overview.get("by_region") or [],
            "by_model": (overview.get("by_model") or [])[:12],
            "hold_buckets": overview.get("hold_buckets") or {},
            "aged_qty": overview.get("aged_qty"),
            "top_aged_stores": overview.get("top_aged_stores") or [],
        },
        "as_of": data.get("as_of_date") or overview.get("as_of_date") or "",
        "note": "overview는 전체 판매점 재고, scope는 이번 질문/지도 필터 결과이다.",
    }


def _answer_from_parsed(
    conn,
    parsed: dict,
    lat: float | None,
    lng: float | None,
    bbox=None,
) -> dict:
    intent = parsed["intent"]
    specified = [m for m in (parsed.get("models") or []) if m]
    if specified:
        model = ",".join(specified)
    elif (parsed.get("model") or "").strip() and (parsed.get("model") or "").strip().lower() not in {"all", "*"}:
        model = parsed.get("model")
    else:
        model = "ALL"
    wanted_bbox = normalize_bbox(bbox)

    if intent == "help":
        if parsed.get("dealer_id"):
            name = parsed.get("dealer_name") or "이 대리점"
            answer = (
                f"{name} 재고를 보여 드립니다. "
                "어디에 몇 대인지, 오래 묵은 재고, 지도에서 고른 영역도 물어볼 수 있습니다. "
                "예: 「오래 묵은 재고 어디가 많아?」, 「김포에 뭐가 있어?」, 「이 영역에 A175 몇 대야」."
            )
        else:
            answer = (
                "재고 현황을 보고 답합니다. 어디에 몇 대인지뿐 아니라 "
                "체화(30일+), 대리점 비교, 어디를 먼저 처리할지까지 물어보세요. "
                "지도에서 「영역 선택」으로 사각형을 그리면 그 안의 기종별 대수도 계산합니다. "
                "예: 「오래 묵은 재고 어디가 많아?」, 「유원이랑 프리스비 비교해줘」, "
                "「김포에 뭐가 있어?」, 「이 영역에 A175 몇 대야」."
            )
        return {
            "intent": intent,
            "model": model,
            "needs_location": False,
            "needs_area": False,
            "answer": answer,
            "speech": answer,
            "map": None,
            "tables": [],
        }

    if parsed.get("use_map_area") and not wanted_bbox:
        answer = "지도 왼쪽 위 「영역 선택」을 누른 뒤, 드래그해서 사각형을 그려 주세요. 그 안의 기종별 재고를 계산합니다."
        return {
            "intent": intent,
            "model": model,
            "needs_location": False,
            "needs_area": True,
            "answer": answer,
            "speech": answer,
            "map": None,
            "tables": [],
        }

    if intent == "nearest" and (lat is None or lng is None):
        answer = "지금 계신 위치를 확인한 뒤, 가장 가까운 판매점 재고를 찾아 드릴게요."
        return {
            "intent": intent,
            "model": model,
            "needs_location": True,
            "needs_area": False,
            "answer": answer,
            "speech": answer,
            "map": None,
            "tables": [],
        }

    dealer_id = parsed.get("dealer_id") or None
    region = parsed.get("region") or ""
    keyword = parsed.get("keyword") or parsed.get("store_code") or ""
    aged_only = bool(parsed.get("aged_only"))
    pin_color = (parsed.get("pin_color") or "").strip()

    if intent == "price":
        code = (parsed.get("store_code") or keyword or "").strip().upper()
        summary = inventory_store_price_sum(conn, code, dealer_id)
        data = inventory_map_points(
            conn,
            "ALL",
            keyword=code,
            dealer_id=dealer_id,
            pin_color=pin_color,
        )
        dealer_scope = f"{parsed['dealer_name']} " if parsed.get("dealer_name") else ""
        as_of = _as_of(data)
        as_of_bit = f" 기준일은 {as_of}입니다." if as_of else ""
        if not summary["qty"]:
            answer = f"{dealer_scope}{code} 판매점 재고를 찾지 못했습니다.{as_of_bit}"
        else:
            name = summary.get("name") or ""
            label = f"{code} {name}".strip()
            won = f"{int(round(summary['total_price'])):,}원"
            answer = f"{dealer_scope}{label} 재고 {summary['qty']}대의 실구매가 합계는 {won}입니다.{as_of_bit}"
            if summary.get("missing_price"):
                answer += f" 가격이 없는 {summary['missing_price']}대는 합계에 넣지 않았습니다."
        data["price_rows"] = summary.get("by_model") or []
        data["price_total"] = summary.get("total_price") or 0
        data["price_qty"] = summary.get("qty") or 0
        return _pack("price", model, answer, data, {}, parsed)

    data = inventory_map_points(
        conn,
        model,
        region=region,
        lat=lat if intent == "nearest" else None,
        lng=lng if intent == "nearest" else None,
        keyword=keyword,
        dealer_id=dealer_id,
        bbox=wanted_bbox,
        aged_only=aged_only,
        pin_color=pin_color,
    )
    overview = {}
    all_models: list[dict] = []
    if intent in {"analyze", "compare", "total", "bbox"}:
        overview = inventory_overview(conn, dealer_id)
    if wanted_bbox or keyword or region or intent in {"analyze", "compare", "bbox"}:
        all_models = inventory_model_breakdown(
            conn,
            dealer_id=dealer_id,
            region=region,
            keyword=keyword,
            bbox=wanted_bbox,
            limit=80,
        )
        data["area_model_totals"] = all_models
    as_of = _as_of(data)
    as_of_bit = f" 기준일은 {as_of}입니다." if as_of else ""
    dealer_scope = f"{parsed['dealer_name']} " if parsed.get("dealer_name") else ""

    if intent == "nearest":
        nearest = data.get("nearest")
        if not nearest:
            answer = f"{dealer_scope}{_model_scope(model)}판매점 재고가 없어 가까운 곳을 찾지 못했습니다."
        else:
            dist = _format_km(nearest.get("distance_meters"))
            addr = " ".join(x for x in [nearest.get("address"), nearest.get("detail_address")] if x)
            dealers_bit = ""
            if nearest.get("shared") and nearest.get("dealers"):
                dealers_bit = " " + ", ".join(
                    f"{d['dealer_name']} {d['qty']}대" for d in nearest["dealers"]
                ) + "."
            answer = (
                f"지금 위치에서 가장 가까운 {dealer_scope}{_model_scope(model)}보유 판매점은 "
                f"{nearest.get('store_code') or ''} {nearest.get('name') or ''}이고, {nearest['qty']}대 있습니다. "
                f"거리는 약 {dist}입니다.{dealers_bit}{_hold_bit(nearest)} {addr}.{as_of_bit}"
            )
        return _pack(intent, model, answer, data, overview, parsed)

    if intent == "region":
        region_name = parsed["region"]
        if data["mapped_qty"] == 0:
            answer = f"{region_name}에서 {dealer_scope}판매점 재고는 없습니다.{as_of_bit}"
        else:
            answer = f"{region_name} {dealer_scope}재고는 {data['mapped_qty']}대, {len(data['points'])}곳입니다. 숫자는 아래 표입니다.{as_of_bit}"
        return _pack(intent, model, answer, data, overview, parsed)

    if intent == "keyword":
        key = parsed["keyword"]
        if data["mapped_qty"] == 0 and not all_models:
            answer = f"「{key}」로 찾은 {dealer_scope}판매점 재고는 없습니다.{as_of_bit}"
        else:
            answer = f"「{key}」 {dealer_scope}재고는 {data['mapped_qty']}대, {len(data['points'])}곳입니다. 숫자는 아래 표입니다.{as_of_bit}"
        return _pack(intent, model, answer, data, overview, parsed)

    if intent in {"aged", "compare", "analyze", "bbox", "total"}:
        scope = "선택한 영역" if wanted_bbox else (keyword or region or "전체")
        if intent == "aged":
            n_stores = len(data.get("points") or [])
            if n_stores == 0:
                answer = f"{dealer_scope}30일 이상 재고를 가진 판매점이 없습니다.{as_of_bit}"
            else:
                answer = f"{dealer_scope}30일 이상 재고를 가진 판매점 {n_stores}곳을 지도에 표시했습니다.{as_of_bit}"
        elif data["mapped_qty"] == 0 and not all_models:
            answer = f"{scope}에서 {dealer_scope}판매점 재고가 없습니다.{as_of_bit}"
        else:
            answer = f"{scope} {dealer_scope}재고는 {data['mapped_qty']}대, {len(data['points'])}곳입니다. 숫자는 아래 표입니다.{as_of_bit}"
        return _pack(intent, model, answer, data, overview, parsed)

    if data["mapped_qty"] == 0:
        answer = f"{dealer_scope}{_model_scope(model)}판매점 재고가 없습니다.{as_of_bit}"
    else:
        answer = f"{dealer_scope}재고는 {data['mapped_qty']}대, {len(data['points'])}곳입니다. 숫자는 아래 표입니다.{as_of_bit}"
    return _pack("total", model, answer, data, overview, parsed)


def _model_totals_bit(models: list[dict], n: int = 5) -> str:
    rows = [m for m in models if m.get("qty")]
    if not rows:
        return ""
    bits = [f"{m['model']} {m['qty']}대" for m in rows[:n]]
    extra = f" 외 {len(rows) - n}기종" if len(rows) > n else ""
    return " 기종별 " + ", ".join(bits) + extra + "."


def _n(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _qty_table(title: str, columns: list[str], rows: list[list], footer: list | None = None) -> dict:
    return {"title": title, "columns": columns, "rows": rows, "footer": footer or []}


def _build_tables(intent: str, parsed: dict, data: dict, overview: dict) -> list[dict]:
    if not data:
        return []
    tables: list[dict] = []
    models = [m for m in (data.get("area_model_totals") or data.get("model_totals") or []) if _n(m.get("qty"))]
    dealers = [d for d in (data.get("dealer_totals") or []) if _n(d.get("qty"))]
    points = list(data.get("points") or [])
    regions = [r for r in (data.get("regions") or []) if _n(r.get("qty"))]
    ov_dealers = [d for d in (overview.get("by_dealer") or []) if _n(d.get("qty"))]
    hold = overview.get("hold_buckets") or {}

    if intent == "price":
        rows = [
            [m.get("model") or "", _n(m.get("qty")), int(round(m.get("amount") or 0))]
            for m in (data.get("price_rows") or [])
            if _n(m.get("qty"))
        ]
        if rows:
            tables.append(
                _qty_table(
                    "기종별 실구매가",
                    ["기종", "대수", "금액(원)"],
                    rows,
                    ["합계", sum(r[1] for r in rows), int(round(data.get("price_total") or 0))],
                )
            )
        return tables
    if intent in {"total", "analyze", "bbox", "keyword", "region", "aged", "compare"} and models:
        rows = [
            [m.get("model") or "", _n(m.get("qty")), _n(m.get("stores")), _n(m.get("aged_qty"))]
            for m in models[:40]
        ]
        tables.append(
            _qty_table(
                "기종별 재고",
                ["기종", "대수", "매장", "30일+"],
                rows,
                ["합계", sum(r[1] for r in rows), "", sum(r[3] for r in rows)],
            )
        )
    if intent in {"compare", "analyze", "total", "aged"} and (ov_dealers or dealers):
        src = ov_dealers if intent in {"compare", "analyze", "total"} and ov_dealers else dealers
        rows = [
            [d.get("dealer_name") or "미지정", _n(d.get("qty")), _n(d.get("stores")), _n(d.get("aged_qty"))]
            for d in src
        ]
        tables.append(
            _qty_table(
                "대리점별 재고",
                ["대리점", "대수", "매장", "30일+"],
                rows,
                ["합계", sum(r[1] for r in rows), "", sum(r[3] for r in rows)],
            )
        )
    if intent in {"analyze", "total"} and regions:
        rows = [[r.get("region") or "", _n(r.get("qty")), _n(r.get("stores"))] for r in regions[:12]]
        tables.append(_qty_table("지역별 재고", ["지역", "대수", "매장"], rows, ["합계", sum(r[1] for r in rows), ""]))
    if intent in {"analyze", "aged"} and hold:
        tables.append(
            _qty_table(
                "보유기간",
                ["구분", "대수"],
                [
                    ["15일 미만", _n(hold.get("under_15"))],
                    ["15~29일", _n(hold.get("days_15_29"))],
                    ["30일 이상", _n(hold.get("days_30_plus"))],
                ],
            )
        )
    if intent == "aged":
        aged_stores = overview.get("top_aged_stores") or []
        if not aged_stores:
            aged_stores = sorted(points, key=lambda p: -_n(p.get("aged_qty")))[:8]
        rows = [
            [
                s.get("store_code") or "",
                s.get("name") or s.get("store_code") or "",
                _n(s.get("aged_qty")),
                _n(s.get("qty")),
                _n(s.get("max_hold_days")),
            ]
            for s in aged_stores
            if _n(s.get("aged_qty"))
        ]
        if rows:
            tables.append(_qty_table("체화 많은 매장", ["P코드", "판매점", "30일+", "전체", "최장(일)"], rows))
    if intent == "nearest" and data.get("nearest"):
        n = data["nearest"]
        tables.append(
            _qty_table(
                "가장 가까운 매장",
                ["P코드", "판매점", "대수", "거리", "30일+"],
                [[
                    n.get("store_code") or "",
                    n.get("name") or "",
                    _n(n.get("qty")),
                    _format_km(n.get("distance_meters")),
                    _n(n.get("aged_qty")),
                ]],
            )
        )
    elif intent in {"keyword", "region", "bbox"} and len(points) >= 1:
        top = sorted(points, key=lambda p: -_n(p.get("qty")))[:8]
        rows = [
            [p.get("store_code") or "", p.get("name") or "", _n(p.get("qty")), _n(p.get("aged_qty"))]
            for p in top
        ]
        tables.append(_qty_table("재고 많은 매장", ["P코드", "판매점", "대수", "30일+"], rows))
    return tables


def _model_scope(model: str) -> str:
    text = (model or "").strip()
    if not text or text.upper() in {"ALL", "*"}:
        return ""
    return f"{text} "


def _pack(intent: str, model: str, answer: str, data: dict, overview: dict, parsed: dict | None = None) -> dict:
    tables = _build_tables(intent, parsed or {}, data or {}, overview or {})
    extra = ""
    if parsed and parsed.get("pin_color"):
        if parsed.get("aged_only"):
            extra = " 보유 30일 이상인 판매점을 요청하신 색으로 지도에 표시했습니다."
        else:
            extra = " 지도 핀을 요청하신 색으로 표시했습니다."
    text = (answer or "") + extra
    return {
        "intent": intent,
        "model": model,
        "needs_location": False,
        "needs_area": False,
        "answer": text,
        "speech": text,
        "map": data,
        "overview": overview,
        "tables": tables,
    }

