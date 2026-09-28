"""재고 챗봇 질문 파싱: keyword에 질문어/일반어가 섞여 매장 검색이 0건으로 새는 버그 회귀 테스트.

interpret_inventory_question()/parse_inventory_question() 은 db.py/app.py 를 쓰지 않으므로
(웹앱 최상단 import 금지 규칙과 무관하게) 이 파일은 모듈 최상단에서 바로 import 해도 된다.
"""

from inventory_chat import parse_inventory_question
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
