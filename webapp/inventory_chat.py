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
    resolve_petname_models,
)
from inventory_llm import interpret_inventory_question, llm_available

DEFAULT_MODEL = ""

# SKT/대리점이 같은 뜻으로 섞어 쓰는 용어. 질문을 파싱하기 전에 표준 단어로 바꿔서,
# 그 표준 단어가 이미 갖고 있는 처리(불용어 drop-list, "이 판매점" 같은 지시어 인식 등)를
# 그대로 물려받는다 — 용어별로 특수 처리를 따로 만들지 않는다.
# 새 용어가 나오면 여기에 한 줄만 추가하면 된다.
TERM_SYNONYMS = {
    "거래처": "판매점",  # 대리점이 거래하는 판매점(매장)을 가리키는 말.
}


def _normalize_terms(text: str) -> str:
    result = text or ""
    for word, canonical in TERM_SYNONYMS.items():
        result = result.replace(word, canonical)
    return result


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
ANALYZE_HINTS = (
    "어때",
    "현황",
    "요약",
    "추천",
    "먼저",
    "문제",
    "분석",
    "어디부터",
    "가장 많은",
    "제일 많은",
)
COLOR_HINTS = ("색으로", "색깔", "색상", "칠해", "표시해", "보여줘", "보여 줘")
# "판매점 리스트 보여줘"처럼 대수(재고량) 대신 매장 자체의 목록을 원하는 질문.
LIST_HINTS = ("리스트", "목록")
# "그 판매점에 무슨 재고 있어?" 처럼 바로 앞 답에서 나온 매장을 가리키는 말.
# 이 말만으로는 매장을 특정할 수 없어서, 프런트가 직전 응답의 대표 매장(store_code)을
# last_store_code 로 함께 보내주면 그 매장으로 좁힌다 (질문 자체엔 P코드/지명이 없어도 됨).
REFER_HINTS = (
    "그 판매점",
    "그판매점",
    "그 매장",
    "그매장",
    "그 곳",
    "그곳",
    "거기",
    "해당 매장",
    "해당매장",
    "해당 판매점",
    "해당판매점",
    "이 매장",
    "이매장",
    "이 판매점",
    "이판매점",
    "방금 그",
    "방금그",
    "아까 그",
    "아까그",
)

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


def _any_hint(compact: str, hints: tuple[str, ...]) -> bool:
    """*_HINTS 튜플에 있는 "장기 보유"처럼 띄어쓰기 있는 힌트를, 공백을 이미 다 지운
    compact 문자열과 비교한다. 힌트마다 "장기보유" 없는 버전을 손으로 또 넣지 않아도
    되게 여기서 한 번에 처리한다 — 실제로 "가장 많은"/"현재 위치" 등 여럿이 이 공백
    불일치 때문에 한 번도 안 걸리고 있었다.
    """
    return any(_compact(h) in compact for h in hints)


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


_COLOR_RULE_CLAUSE_RE = re.compile(r"[,，]|그리고|(?<=\S)이고(?=\s)")
_DAY_RANGE_RE = re.compile(r"(\d+)\s*(?:일)?\s*(?:[~\-]|부터)\s*(\d+)\s*일")
_DAY_MAX_RE = re.compile(r"(\d+)\s*일\s*(이하|미만|이내|까지)")
_DAY_MIN_RE = re.compile(r"(\d+)\s*일\s*(이상|초과)")


def _extract_color_rules(text: str) -> list[dict]:
    """"10일 이하는 초록색, 10~20일은 노란색, 20일 이상은 빨간색으로 표시해줘" 처럼
    사용자가 직접 정한 보유기간 구간별 색을 [{min, max, color}, ...] 로 뽑는다.

    쉼표·"그리고"로 나눈 조각마다 구간과 색을 하나씩 찾는다. 구간이나 색 중 하나라도
    없는 조각은 버린다 — 애매한 값으로 임의로 추정하지 않는다. max=None 이면 그 이상은
    다 포함(상한 없음)이라는 뜻이다.
    """
    rules: list[dict] = []
    for clause in _COLOR_RULE_CLAUSE_RE.split(text or ""):
        color = _extract_pin_color(clause)
        if not color:
            continue
        m = _DAY_RANGE_RE.search(clause)
        if m:
            lo, hi = sorted((int(m.group(1)), int(m.group(2))))
            rules.append({"min": lo, "max": hi, "color": color})
            continue
        m = _DAY_MAX_RE.search(clause)
        if m:
            n = int(m.group(1))
            hi = n - 1 if m.group(2) == "미만" else n
            rules.append({"min": 0, "max": max(hi, 0), "color": color})
            continue
        m = _DAY_MIN_RE.search(clause)
        if m:
            n = int(m.group(1))
            lo = n + 1 if m.group(2) == "초과" else n
            rules.append({"min": lo, "max": None, "color": color})
            continue
    return rules


def _color_rules_bit(rules: list[dict]) -> str:
    if not rules:
        return ""
    parts = []
    for r in rules:
        if r.get("max") is None:
            parts.append(f"{r['min']}일 이상 {r['color']}")
        elif r.get("min") == 0:
            parts.append(f"{r['max']}일 이하 {r['color']}")
        else:
            parts.append(f"{r['min']}~{r['max']}일 {r['color']}")
    return " 요청하신 구간별 색상으로 지도에 표시했습니다: " + ", ".join(parts) + "."


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


def _extract_store_code(text: str, last_store_code: str = "") -> str:
    # 실제 P코드는 "PE2810"/"PC0198"처럼 P + 영문자 1개 + 숫자 형태도 많다.
    # 숫자만 있는 P\d+ 만 잡으면 이런 코드를 그대로 타이핑해도 못 알아듣는다.
    match = re.search(r"P\s*[A-Za-z]?\s*\d{4,}", text or "", re.I)
    if match:
        return re.sub(r"\s+", "", match.group(0)).upper()
    compact = _compact(text)
    if last_store_code and _any_hint(compact, REFER_HINTS):
        return last_store_code.strip().upper()
    return ""


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
        "알려줘",
        "확인",
        "보여",
        "보여줘",
        "찾아",
        "위치",
        "기준",
        "가장",
        "가까운",
        "근처",
        "어디",
        "많아",
        "많이",
        "많나요",
        "얼마",
        "얼마나",
        "추천",
        "궁금",
        "궁금해",
        "무엇",
        "뭐",
        "뭐가",
        "무슨",
        "어느",
        "있는지",
        "있나요",
        "있는",
        "곳의",
        "곳",
        "판매점",
        "리스트",
        "목록",
        "보유한",
        "보유중",
        "보유하고",
        "보유",
        "갖고있는",
        "갖고 있는",
        "가진",
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
        # 색상 요청("10일 이하는 초록색으로")에서 색 이름·구간 연결어가 지명으로 오인되지 않게.
        "이하",
        "이상",
        "미만",
        "초과",
        "이내",
        "까지",
        "부터",
        "구간",
        "표시해",
        "칠해",
        "칠해줘",
        "표시",
    ]
    drop.extend(word for word, _ in _PIN_COLORS)
    for word in extra_drop or []:
        if word:
            drop.append(word)
    for word in drop:
        if word:
            cleaned = cleaned.replace(word, " ")
    cleaned = re.sub(r"[^0-9A-Za-z가-힣\s]", " ", cleaned)
    # "10일"처럼 순수 숫자(+일)만 남은 토큰은 지명·매장명일 수 없다 (색상 구간 경계 등).
    tokens = [t for t in cleaned.split() if len(t) >= 2 and not re.fullmatch(r"\d+일?", t)]
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


def _keyword_matches_any_store(conn, keyword: str) -> bool:
    """keyword가 실제 판매점 이름/주소/코드에 있는 말인지 DB로 확인한다.

    "이 지명/매장명이 진짜 있는가"를 실제 데이터로 검증하는 것이라, 새로운 일반 단어가
    keyword로 새어나올 때마다 불용어를 하나씩 추가하는 것보다 근본적인 방어다.
    """
    like = f"%{keyword}%"
    row = conn.execute(
        "SELECT 1 FROM stores WHERE name LIKE ? OR address LIKE ? OR detail_address LIKE ? OR store_code LIKE ? LIMIT 1",
        (like, like, like, like),
    ).fetchone()
    return row is not None


def _lookup_store(conn, code: str) -> dict | None:
    row = conn.execute(
        "SELECT store_code, name, address, detail_address FROM stores WHERE store_code = ?",
        (code,),
    ).fetchone()
    return dict(row) if row else None


def _top_stores_bit(points: list, n: int = 3) -> str:
    if not points:
        return ""
    ranked = sorted(points, key=lambda p: p.get("qty") or 0, reverse=True)[:n]
    bits = [f"{p['name']} {p['qty']}대" for p in ranked]
    return " 많은 곳부터 " + ", ".join(bits) + "입니다."


def parse_inventory_question(
    text: str, dealers: list[dict] | None = None, last_store_code: str = ""
) -> dict:
    raw = _normalize_terms((text or "").strip())
    compact = _compact(raw)
    model = _extract_model(raw)
    region = _extract_region(raw)
    dealer = _extract_dealer(raw, dealers or [])
    store_code = _extract_store_code(raw, last_store_code)
    extra = []
    if dealer:
        extra.extend([dealer.get("name") or "", dealer.get("dealer_code") or "", "대리점"])
    keyword = _extract_keyword(raw, region, extra)
    if store_code:
        keyword = store_code

    has_price = _any_hint(compact, PRICE_HINTS) or (
        store_code and "합" in compact and "얼마" in compact
    )
    if _any_hint(compact, HELP_HINTS) or not compact:
        intent = "help"
    elif store_code and has_price:
        intent = "price"
    elif _any_hint(compact, AREA_HINTS):
        intent = "bbox"
    elif _any_hint(compact, NEAREST_HINTS):
        intent = "nearest"
    elif _any_hint(compact, AGED_HINTS):
        intent = "aged"
    elif _any_hint(compact, COMPARE_HINTS):
        intent = "compare"
    elif _any_hint(compact, ANALYZE_HINTS):
        intent = "analyze"
    elif region:
        intent = "region"
    elif dealer and (_any_hint(compact, TOTAL_HINTS) or not keyword):
        intent = "total"
    elif _any_hint(compact, TOTAL_HINTS) or not keyword:
        intent = "total"
    else:
        intent = "keyword"

    return {
        "intent": intent,
        "model": model or "ALL",
        "models": [model] if model else [],
        "region": region if intent in {"region", "nearest", "analyze", "aged", "compare", "bbox"} else "",
        # "서울 중구에 있는 판매점 알려줘"처럼 지역명(서울)과 더 좁은 지명(중구)이 같이 오면,
        # 지역이 잡혔다고 keyword를 버리지 않는다 — 시/도 단위보다 더 좁혀서 같이 거른다.
        "keyword": (
            keyword
            if intent in {"keyword", "nearest", "analyze", "aged", "compare", "bbox", "price", "region"}
            else ""
        ),
        "store_code": store_code,
        "dealer_id": dealer["id"] if dealer else "",
        "dealer_name": dealer.get("name") or "" if dealer else "",
        "raw": raw,
        "needs_location": intent == "nearest",
        "use_map_area": intent == "bbox",
        "aged_only": intent == "aged",
        "pin_color": _extract_pin_color(raw),
        "list_mode": _any_hint(compact, LIST_HINTS),
        "nlu": "rules",
    }


def ask_inventory(
    conn,
    text: str,
    lat: float | None = None,
    lng: float | None = None,
    bbox=None,
    dealer_id: str | None = None,
    last_store_code: str | None = None,
) -> dict:
    # "거래처" 같은 SKT/대리점 용어를 표준 단어로 맞춰서, 규칙 기반이든 LLM이든
    # 이 함수가 부르는 모든 추출기가 같은 말을 보게 한다.
    text = _normalize_terms(text)
    all_dealers = [dict(r) for r in conn.execute("SELECT id, dealer_code, name FROM dealers").fetchall()]
    dealers = [d for d in all_dealers if d["id"] == dealer_id] if dealer_id else all_dealers
    nlu = "rules"
    rules = parse_inventory_question(text, dealers, last_store_code=(last_store_code or "").strip())
    if not rules.get("models"):
        # SM-코드로 못 알아들은 질문("플립7", "갤럭시 S25" 처럼 펫네임으로만 부른 경우)은
        # 모델 조회 표(관리자가 올린 영업정책 모델 조회.xlsx)에서 대표모델을 찾아본다.
        petname_models = resolve_petname_models(conn, text)
        if petname_models:
            rules["models"] = petname_models
            rules["model"] = ",".join(petname_models)
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
    color_rules = _extract_color_rules(text)
    if color_rules:
        parsed["pin_color_rules"] = color_rules
        # "30일 이상은 빨간색"처럼 구간 경계에 쓴 "30일"이 AGED_HINTS와 우연히 겹쳐
        # aged_only 필터가 걸리면, 다른 구간(예: 10~20일) 매장이 지도에서 통째로 빠진다.
        # "체화/오래/묵은"처럼 정말 체화 재고만 보겠다는 말이 따로 없으면 필터는 걸지 않는다.
        explicit_aged_words = ("오래", "묵은", "체화", "장기보유", "장기 보유", "보유기간", "출고된지")
        if parsed.get("intent") == "aged" and not any(w in text for w in explicit_aged_words):
            parsed["intent"] = "analyze" if (dealer_id or parsed.get("dealer_id")) else "total"
            parsed["aged_only"] = False
    else:
        color = _extract_pin_color(text)
        if color:
            parsed["pin_color"] = color
            compact = _compact(text)
            if _any_hint(compact, AGED_HINTS) or _any_hint(compact, COLOR_HINTS):
                if _any_hint(compact, AGED_HINTS):
                    parsed["aged_only"] = True
    parsed["nlu"] = nlu
    if dealer_id:
        scoped = next((d for d in all_dealers if d["id"] == dealer_id), None)
        parsed["dealer_id"] = dealer_id
        parsed["dealer_name"] = (scoped or {}).get("name") or parsed.get("dealer_name") or ""
        if parsed.get("intent") == "compare":
            parsed["intent"] = "analyze"
    if parsed.get("intent") == "keyword" and not parsed.get("store_code"):
        # keyword는 "판매점명/주소에 이 말이 들어간 곳"을 찾는 필터라, 새 단어가 나올 때마다
        # 불용어 목록에 하나씩 추가하는 건 끝이 없다("어디"/"보유한" 등 실제로 반복됐다).
        # 대신 실제 판매점 데이터에 그 말이 있는지 먼저 확인해서, 없으면(=지명/매장명이
        # 아니라 그냥 새어나온 일반 단어일 가능성이 높으면) keyword를 버리고 전체로 돌아간다.
        # store_code(P코드 직접 입력, "그 판매점" 지시어)로 정해진 경우는 그대로 둔다 —
        # 그건 사용자가 특정 매장을 콕 집은 것이라 "결과 없음"이 맞는 답일 수 있다.
        kw = (parsed.get("keyword") or "").strip()
        if kw and not _keyword_matches_any_store(conn, kw):
            parsed["keyword"] = ""
            parsed["intent"] = "total"
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
    pin_color_rules = parsed.get("pin_color_rules") or []

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
        data["pin_color_rules"] = pin_color_rules
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
    data["pin_color_rules"] = pin_color_rules
    overview = {}
    all_models: list[dict] = []
    if intent in {"analyze", "compare", "total", "bbox"}:
        overview = inventory_overview(conn, dealer_id)
    if wanted_bbox or keyword or region or intent in {"analyze", "compare", "bbox"}:
        # 기종을 특정해서 물었으면("김포에 S931 얼마나 있어") 그 영역의 다른 기종까지
        # 끼워 보여줄 필요가 없다 — 물어본 기종(들)만 집계한다. 전체를 물었을 때만
        # (data["models"]가 비어 있을 때) 영역 안 모든 기종을 보여준다.
        all_models = inventory_model_breakdown(
            conn,
            dealer_id=dealer_id,
            region=region,
            keyword=keyword,
            bbox=wanted_bbox,
            limit=80,
            models=data.get("models") or None,
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

    list_mode = bool(parsed.get("list_mode"))

    if intent == "region":
        region_name = parsed["region"]
        if list_mode:
            n_stores = len(data.get("points") or [])
            answer = (
                f"{region_name}에 {dealer_scope}판매점이 없습니다.{as_of_bit}"
                if n_stores == 0
                else f"{region_name} {dealer_scope}판매점은 {n_stores}곳입니다. 목록은 아래 표와 지도를 확인하세요.{as_of_bit}"
            )
        elif data["mapped_qty"] == 0:
            answer = f"{region_name}에서 {dealer_scope}판매점 재고는 없습니다.{as_of_bit}"
        else:
            answer = f"{region_name} {dealer_scope}재고는 {data['mapped_qty']}대, {len(data['points'])}곳입니다. 숫자는 아래 표입니다.{as_of_bit}"
        return _pack(intent, model, answer, data, overview, parsed)

    if intent == "keyword":
        key = parsed["keyword"]
        if list_mode:
            n_stores = len(data.get("points") or [])
            answer = (
                f"「{key}」로 찾은 {dealer_scope}판매점이 없습니다.{as_of_bit}"
                if n_stores == 0
                else f"「{key}」 {dealer_scope}판매점은 {n_stores}곳입니다. 목록은 아래 표와 지도를 확인하세요.{as_of_bit}"
            )
        elif data["mapped_qty"] == 0 and not all_models:
            # P코드로 직접 물었는데 0건이면 "그런 코드가 아예 없다"와 "코드는 있는데
            # 지금 올라온 재고가 0이다"를 구분해 답한다 — 둘 다 그냥 "재고는 없습니다"로
            # 뭉뚱그리면, 판매점 마스터엔 있는 진짜 코드도 마치 시스템이 그 코드 자체를
            # 모르는 것처럼 보인다.
            store_code = (parsed.get("store_code") or "").strip()
            store = _lookup_store(conn, store_code) if store_code else None
            if store:
                addr = " ".join(x for x in [store.get("address"), store.get("detail_address")] if x)
                answer = (
                    f"{store_code} {store.get('name') or ''}은(는) 등록된 판매점이지만, "
                    f"{dealer_scope}현재 업로드된 재고가 없습니다.{f' {addr}.' if addr else ''}{as_of_bit}"
                )
            elif store_code:
                answer = f"{store_code} 코드를 가진 판매점을 찾지 못했습니다. 코드를 다시 확인해주세요."
            else:
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
        if intent == "analyze":
            # "가장 많은 재고를 보유한 판매점" 처럼 1위를 콕 집어 물었을 수 있으니,
            # 전체 요약과 별개로 1위 매장 이름을 답 문장 자체에 바로 넣어준다.
            top_stores = [s for s in (overview.get("top_stores") or []) if _n(s.get("qty"))]
            if top_stores:
                top = top_stores[0]
                answer += (
                    f" 재고가 가장 많은 판매점은 {top.get('store_code') or ''} {top.get('name') or ''}"
                    f"({_n(top.get('qty'))}대)입니다."
                )
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
    # "판매점 리스트/목록 보여줘"는 대수 집계가 아니라 매장 자체를 원한 질문이다 —
    # 기종별/랭킹 표 대신 주소가 포함된 전체 매장 목록 하나만 보여준다.
    if parsed.get("list_mode") and intent in {"keyword", "region", "bbox"}:
        points = list(data.get("points") or [])
        ranked = sorted(points, key=lambda p: (p.get("name") or p.get("store_code") or ""))
        rows = [
            [
                p.get("store_code") or "",
                p.get("name") or "",
                " ".join(x for x in [p.get("address"), p.get("detail_address")] if x),
                _n(p.get("qty")),
            ]
            for p in ranked[:200]
        ]
        if rows:
            tables.append(_qty_table("판매점 목록", ["P코드", "판매점", "주소", "대수"], rows))
        return tables
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
    if intent in {"analyze", "total"}:
        top_stores = [s for s in (overview.get("top_stores") or []) if _n(s.get("qty"))]
        if top_stores:
            rows = [
                [s.get("store_code") or "", s.get("name") or "", _n(s.get("qty")), _n(s.get("aged_qty"))]
                for s in top_stores[:8]
            ]
            tables.append(_qty_table("재고 많은 매장", ["P코드", "판매점", "대수", "30일+"], rows))
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
    if parsed and parsed.get("pin_color_rules"):
        extra = _color_rules_bit(parsed["pin_color_rules"])
    elif parsed and parsed.get("pin_color"):
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

