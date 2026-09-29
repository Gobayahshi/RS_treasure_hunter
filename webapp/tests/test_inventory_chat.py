"""재고 챗봇 질문 파싱: keyword에 질문어/일반어가 섞여 매장 검색이 0건으로 새는 버그 회귀 테스트.

interpret_inventory_question()/parse_inventory_question() 은 db.py/app.py 를 쓰지 않으므로
(웹앱 최상단 import 금지 규칙과 무관하게) 이 파일은 모듈 최상단에서 바로 import 해도 된다.
"""

from inventory_chat import _extract_color_rules, _normalize_terms, parse_inventory_question
from inventory_llm import _clean_keyword, interpret_inventory_question


def test_parse_inventory_question_drops_question_word_as_keyword():
    parsed = parse_inventory_question("SM-F971 어디 많아")
    assert parsed["model"] == "SM-F971"
    assert parsed["keyword"] == ""
    # keyword가 없으니 매장명 검색(intent=keyword)이 아니라 전체 집계로 빠져야 한다.
    assert parsed["intent"] != "keyword"


def test_parse_inventory_question_keeps_real_place_as_keyword():
    parsed = parse_inventory_question("김포에 뭐가 있어")
    assert parsed["keyword"] == "김포"
    assert parsed["intent"] == "keyword"


def test_clean_keyword_drops_question_words():
    for word in ["어디", "어디에", "많아", "재고", "보여줘", "얼마", "추천"]:
        assert _clean_keyword(word) == ""


def test_clean_keyword_keeps_real_place_names():
    assert _clean_keyword("김포") == "김포"
    assert _clean_keyword("강남대리점") == "강남대리점"


def test_interpret_inventory_question_drops_llm_question_word_keyword(monkeypatch):
    import inventory_llm

    monkeypatch.setattr(
        inventory_llm,
        "_chat",
        lambda *a, **k: '{"intent":"keyword","models":["SM-F971"],"keyword":"어디"}',
    )
    parsed = interpret_inventory_question("SM-F971 어디 많아", [])
    assert parsed["model"] == "SM-F971"
    assert parsed["keyword"] == ""


def test_parse_inventory_question_resolves_referring_pronoun_to_last_store():
    """"그 판매점에 무슨 재고 있어?" 는 지명/코드가 없어 이전엔 전체 집계로 빠졌다.

    프런트가 직전 응답의 대표 매장(store_code)을 last_store_code 로 같이 보내면
    "그 판매점"/"거기" 같은 말을 그 매장을 가리키는 것으로 본다.
    """
    parsed = parse_inventory_question("그 판매점에 무슨 재고가 있어", last_store_code="PC0198")
    assert parsed["store_code"] == "PC0198"
    assert parsed["keyword"] == "PC0198"
    assert parsed["intent"] == "keyword"


def test_parse_inventory_question_ignores_referring_pronoun_without_last_store():
    parsed = parse_inventory_question("그 판매점에 무슨 재고가 있어")
    assert parsed["store_code"] == ""
    assert parsed["keyword"] == ""


def test_parse_inventory_question_explicit_store_code_wins_over_last_store():
    parsed = parse_inventory_question("PC9999에 무슨 재고 있어", last_store_code="PC0198")
    assert parsed["store_code"] == "PC9999"


def test_parse_inventory_question_recognizes_letter_prefixed_store_code():
    """실제 P코드는 'PE2810'처럼 P+영문자+숫자 형태가 흔한데, 숫자만 잡던 정규식이 놓치고 있었다."""
    parsed = parse_inventory_question("PE2810 재고 얼마나 있어")
    assert parsed["store_code"] == "PE2810"


def test_normalize_terms_maps_georaecheo_to_store():
    # 단순 문자열 치환이라 조사(가/이)는 안 맞춰준다 — 어차피 _extract_keyword가
    # 조사를 따로 걸러내므로 파싱 결과에는 영향이 없다.
    assert _normalize_terms("거래처가 어디 있어") == "판매점가 어디 있어"


def test_parse_inventory_question_treats_georaecheo_as_store_synonym():
    """"거래처"는 대리점이 거래하는 판매점을 가리키는 말 — "판매점"과 똑같이 다룬다.

    "판매점"처럼 일반 단어라 keyword로 새지 않아야 하고("그 판매점" 회귀 테스트와 같은 원리),
    지시어("이 거래처")도 "이 판매점"과 같은 REFER_HINTS를 그대로 물려받아야 한다.
    """
    parsed = parse_inventory_question("거래처가 어디 있어")
    assert parsed["keyword"] == ""

    parsed = parse_inventory_question("이 거래처에 무슨 재고 있어", last_store_code="PC0198")
    assert parsed["store_code"] == "PC0198"


def test_parse_inventory_question_keeps_district_keyword_alongside_region():
    """"서울 중구에 있는 판매점 리스트 보여줘"는 시/도(서울)만 잡히고 구(중구)는 버려져
    서울 전체로 뭉뚱그려 나가던 버그. region이 잡혀도 keyword(중구)를 같이 남긴다."""
    parsed = parse_inventory_question("서울 중구에 있는 판매점 리스트 보여줘")
    assert parsed["intent"] == "region"
    assert parsed["region"] == "서울"
    assert parsed["keyword"] == "중구"
    assert parsed["list_mode"] is True


def test_parse_inventory_question_list_mode_off_by_default():
    parsed = parse_inventory_question("서울 재고 몇 대야")
    assert parsed["list_mode"] is False


def test_extract_color_rules_parses_multiple_ranges():
    rules = _extract_color_rules("10일 이하는 초록색, 10~20일은 노란색, 20일 이상은 빨간색으로 표시해줘")
    assert rules == [
        {"min": 0, "max": 10, "color": "#16a34a"},
        {"min": 10, "max": 20, "color": "#ca8a04"},
        {"min": 20, "max": None, "color": "#dc2626"},
    ]


def test_extract_color_rules_handles_exclusive_boundaries():
    rules = _extract_color_rules("15일 미만은 파란색, 15일 초과는 빨간색으로 칠해줘")
    assert rules == [
        {"min": 0, "max": 14, "color": "#2563eb"},
        {"min": 16, "max": None, "color": "#dc2626"},
    ]


def test_extract_color_rules_drops_clause_missing_color_or_range():
    # 색이 없는 조각, 구간이 없는 조각은 버린다 — 임의로 추정하지 않는다.
    assert _extract_color_rules("초록색으로 표시해줘") == []
    assert _extract_color_rules("10일 이하는 표시해줘") == []


def test_extract_color_rules_returns_empty_for_plain_questions():
    assert _extract_color_rules("김포에 뭐가 있어") == []


def test_parse_inventory_question_ignores_color_words_as_keyword():
    """색상 요청 문장에서 '초록색' 같은 색 이름이 매장명 검색어로 새면 안 된다."""
    parsed = parse_inventory_question("10일 이하는 초록색, 20일 이상은 빨간색으로 표시해줘")
    assert parsed["keyword"] == ""


def test_ask_inventory_avoids_aged_filter_from_color_boundary(server):
    """색상 구간 경계에 쓴 "30일"이 체화(aged) 필터를 걸어 다른 구간 매장이 통째로
    빠지면 안 된다 — "체화/오래/묵은" 같은 말이 따로 없으면 aged_only를 걸지 않는다."""
    from db import db_session
    from inventory_chat import ask_inventory

    with db_session() as conn:
        result = ask_inventory(
            conn, "10일 이하는 초록색, 30일 이상은 빨간색으로 표시해줘"
        )
        assert result["map"]["aged_only"] is False
        assert result["map"]["pin_color_rules"] == [
            {"min": 0, "max": 10, "color": "#16a34a"},
            {"min": 30, "max": None, "color": "#dc2626"},
        ]


def test_parse_inventory_question_drops_more_leaked_stopwords():
    """"거래처/보유한" 이후 실사용 중 더 찾은 새는 단어들. "매장"/"대리점" 자체가
    일반 단어 drop-list에 없었던 게 빠진 부분이었다."""
    assert parse_inventory_question("우리 매장 재고 알려줘")["keyword"] == ""
    assert parse_inventory_question("이 대리점 재고 몇 대야")["keyword"] == ""
    assert parse_inventory_question("전체 몇 곳이야")["keyword"] == ""


def test_extract_keyword_particle_stripping_does_not_mangle_whole_words():
    """조사 제거를 문자열 아무데서나 지우면 "이월상품"의 "이"를 지워 "월상품"이
    되는 것처럼 단어 자체가 망가진다. 조사는 토큰 "끝"에서만 떼야 한다."""
    parsed = parse_inventory_question("이월상품 있어?")
    assert parsed["keyword"] == "이월상품"


def test_extract_keyword_strips_trailing_particle_correctly():
    parsed = parse_inventory_question("김포에 뭐가 있어")
    assert parsed["keyword"] == "김포"
