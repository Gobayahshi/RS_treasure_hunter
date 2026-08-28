"""생성형 AI로 재고 질문 의도를 읽고, 사실(DB 결과)만으로 답을 다듬는다.

재고 숫자는 모델이 만들지 않는다. 키가 없거나 호출이 실패하면 None을 돌려
규칙 기반 파서로 넘어간다.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

from inventory import REGION_PREFIXES

_ENV_LOADED = False


def _load_local_env() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    path = Path(__file__).resolve().parent / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def llm_available() -> bool:
    _load_local_env()
    return bool(
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("LLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
    )


def _provider() -> str:
    _load_local_env()
    explicit = (os.environ.get("LLM_PROVIDER") or "").strip().lower()
    if explicit:
        return explicit
    if os.environ.get("GEMINI_API_KEY"):
        return "gemini"
    if os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY"):
        return "openai"
    return ""


def _chat(messages: list[dict], timeout: int = 12, json_mode: bool = False) -> str | None:
    provider = _provider()
    if provider == "gemini":
        return _chat_gemini(messages, timeout=timeout, json_mode=json_mode)
    if provider == "openai":
        return _chat_openai(messages, timeout=timeout)
    if os.environ.get("GEMINI_API_KEY"):
        return _chat_gemini(messages, timeout=timeout, json_mode=json_mode)
    return _chat_openai(messages, timeout=timeout)


def _chat_openai(messages: list[dict], timeout: int = 12) -> str | None:
    _load_local_env()
    key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    if not key:
        return None
    base = (os.environ.get("LLM_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("LLM_MODEL") or "gpt-4o-mini"
    payload = json.dumps(
        {
            "model": model,
            "temperature": 0,
            "messages": messages,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    choices = body.get("choices") or []
    if not choices:
        return None
    return ((choices[0].get("message") or {}).get("content") or "").strip() or None


def _chat_gemini(messages: list[dict], timeout: int = 12, json_mode: bool = False) -> str | None:
    _load_local_env()
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("LLM_API_KEY") or ""
    if not key:
        return None
    model = os.environ.get("LLM_MODEL") or os.environ.get("GEMINI_MODEL") or "gemini-3.1-flash-lite"
    system = ""
    contents = []
    for msg in messages:
        role = msg.get("role") or "user"
        text = msg.get("content") or ""
        if role == "system":
            system = text
            continue
        contents.append(
            {
                "role": "user" if role == "user" else "model",
                "parts": [{"text": text}],
            }
        )
    if not contents:
        return None
    body: dict = {
        "contents": contents,
        "generationConfig": {
            "temperature": 0,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"
    payload = json.dumps(body).encode("utf-8")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    candidates = data.get("candidates") or []
    if not candidates:
        return None
    parts = ((candidates[0].get("content") or {}).get("parts") or [])
    texts = [p.get("text") or "" for p in parts if p.get("text")]
    return "\n".join(texts).strip() or None


def _parse_json_content(text: str) -> dict | None:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?", "", raw, flags=re.I).strip()
        raw = re.sub(r"```$", "", raw).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.S)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def _normalize_model(raw: str) -> str:
    model = (raw or "").strip().upper().replace(" ", "")
    if not model:
        return ""
    if model.startswith("SM") and not model.startswith("SM-"):
        return "SM-" + model[2:]
    if model.startswith("SM-") or model.startswith("SM"):
        return model if model.startswith("SM-") else "SM-" + model[2:]
    return model


def _match_dealer(dealer_name: str, dealers: list[dict]) -> dict | None:
    compact = re.sub(r"\s+", "", dealer_name or "").lower()
    if not compact:
        return None
    for item in dealers:
        name = re.sub(r"\s+", "", item.get("name") or "").lower()
        code = (item.get("dealer_code") or "").lower()
        if name and (name in compact or compact in name):
            return item
        if code and code == compact:
            return item
    return None


def interpret_inventory_question(text: str, dealers: list[dict]) -> dict | None:
    regions = [name for name, _ in REGION_PREFIXES]
    dealer_lines = [
        f"{d.get('name') or ''} ({d.get('dealer_code') or ''})".strip()
        for d in dealers
        if d.get("name") or d.get("dealer_code")
    ]
    system = (
        "당신은 휴대폰 판매점 재고 질문의 의도를 JSON으로만 추출한다. "
        "설명 없이 JSON 객체만 출력한다. 숫자는 추측하지 않는다.\n"
        "허용 intent: help, nearest, region, keyword, total, analyze, compare, aged, bbox\n"
        "기종을 말하지 않으면 models는 빈 배열(전체 기종)이다.\n"
        f"지역: {', '.join(regions)}\n"
        f"대리점: {', '.join(dealer_lines[:40])}\n"
        "스키마: "
        '{"intent":"analyze","models":[],"region":"","keyword":"","dealer_name":"",'
        '"needs_location":false,"use_map_area":false,"aged_only":false,"pin_color":""}\n'
        "규칙:\n"
        "- 가까운/근처/내 위치면 nearest, needs_location=true\n"
        "- 지도에서 고른 영역/이 박스/선택한 영역이면 bbox, use_map_area=true\n"
        "- 시·도만 물으면 region\n"
        "- 김포·강남·매장명 등 구체 지명이면 keyword\n"
        "- 오래/묵은/체화/30일/보유기간이 길면 aged, aged_only=true\n"
        "- 비교/어디가 더/두 대리점이면 compare\n"
        "- 어디를 먼저/추천/요약/현황/문제면 analyze\n"
        "- 전체 몇 대면 total\n"
        "- 도움이면 help\n"
        "- models는 사용자가 말한 기종만. 대표상품명·모델명 그대로. 없으면 빈 배열(전체).\n"
        "- 빨강/주황/파랑 등 색으로 보여달라면 pin_color에 CSS hex(예: #dc2626)\n"
        "- 폴드/F971=SM-F971, A175=SM-A175N, S931=SM-S931N"
    )
    content = _chat(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ],
        json_mode=True,
        timeout=8,
    )
    data = _parse_json_content(content or "")
    if not data:
        return None
    intent = (data.get("intent") or "").strip().lower()
    allowed = {
        "help",
        "nearest",
        "region",
        "keyword",
        "total",
        "analyze",
        "compare",
        "aged",
        "bbox",
    }
    if intent not in allowed:
        intent = "analyze"
    raw_models = data.get("models")
    if isinstance(raw_models, str):
        raw_models = [raw_models]
    models = []
    for item in raw_models or []:
        name = _normalize_model(str(item))
        if name and name not in models:
            models.append(name)
    if not models:
        single = _normalize_model(str(data.get("model") or ""))
        if single:
            models.append(single)
    region = (data.get("region") or "").strip()
    keyword = (data.get("keyword") or "").strip()
    dealer_name = (data.get("dealer_name") or "").strip()
    dealer = _match_dealer(dealer_name, dealers)
    use_map_area = bool(data.get("use_map_area")) or intent == "bbox"
    aged_only = bool(data.get("aged_only")) or intent == "aged"
    if intent in {"keyword", "nearest"}:
        keep_keyword = keyword
    elif intent == "analyze" and keyword:
        keep_keyword = keyword
    else:
        keep_keyword = keyword if intent in {"bbox", "aged", "compare"} else ""
    keep_region = region if intent in {"region", "nearest", "analyze", "aged", "compare", "bbox"} else ""
    return {
        "intent": intent,
        "model": ",".join(models) if models else "ALL",
        "models": models,
        "region": keep_region,
        "keyword": keep_keyword,
        "dealer_id": dealer["id"] if dealer else "",
        "dealer_name": (dealer.get("name") if dealer else "") or dealer_name,
        "raw": text,
        "needs_location": bool(data.get("needs_location")) or intent == "nearest",
        "use_map_area": use_map_area,
        "aged_only": aged_only,
        "pin_color": str(data.get("pin_color") or "").strip(),
        "nlu": "llm",
    }


def answer_inventory_from_facts(question: str, facts: dict) -> str | None:
    system = (
        "당신은 SKT 대리점 재고를 돕는 분석가다. facts JSON만 근거로 한국어로 답한다.\n"
        "- 숫자는 facts에 있는 것만 쓴다. 없으면 '자료에 없습니다'라고 한다.\n"
        "- 비교, 30일 이상 체화, 어디가 많은지, 어디를 먼저 처리할지 사실 기반으로 제안한다.\n"
        "- 판매 속도, 입고 예정, 가격, 수요 예측은 자료에 없으면 추측하지 않는다.\n"
        "- 지도에 보이는 범위(기종·지역·영역)를 한 줄로 짚어 준다.\n"
        "- 4~8문장. 필요하면 짧은 불릿.\n"
        "- 재고는 업로드한 대리점 소유이며, 같은 매장에 여러 대리점 재고가 있을 수 있다."
    )
    content = _chat(
        [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": json.dumps(
                    {"question": question, "facts": facts},
                    ensure_ascii=False,
                ),
            },
        ],
        timeout=25,
    )
    text = (content or "").strip()
    if not text or text.startswith("{"):
        return None
    return text


def polish_inventory_answer(question: str, facts: dict, draft: str) -> str | None:
    return answer_inventory_from_facts(
        question,
        {**facts, "draft": draft} if draft else facts,
    )
