from unittest.mock import MagicMock
from extract_terminology import (
    build_batches,
    dedupe_new_terms,
    format_rule_block_text,
    extract_rule_statement,
    ensure_checkbox_property,
    ensure_category_property,
    EXTRACTED_PROPERTY,
    CATEGORY_PROPERTY,
)


def test_build_batches_splits_by_size():
    releases = [{"id": i} for i in range(7)]
    batches = build_batches(releases, 3)
    assert len(batches) == 3
    assert [len(b) for b in batches] == [3, 3, 1]


def test_build_batches_empty_input():
    assert build_batches([], 5) == []


def test_dedupe_new_terms_skips_known():
    known = {"既存用語"}
    candidates = [{"ja": "既存用語"}, {"ja": "新規用語"}]
    result = dedupe_new_terms(candidates, known)
    assert result == [{"ja": "新規用語"}]
    assert known == {"既存用語", "新規用語"}


def test_dedupe_new_terms_dedupes_within_same_batch():
    known = set()
    candidates = [{"ja": "A"}, {"ja": "A"}]
    result = dedupe_new_terms(candidates, known)
    assert result == [{"ja": "A"}]


def test_dedupe_new_terms_skips_blank_ja():
    known = set()
    result = dedupe_new_terms([{"ja": ""}, {"ja": "  "}], known)
    assert result == []
    assert known == set()


def test_format_rule_block_text_includes_example_and_sources():
    rule = {
        "rule": "タイトル名は『』で囲む",
        "example": "PUBG→『PUBG』",
        "sources": [{"title": "リリースA", "url": "https://prtimes.jp/a"}],
    }
    text = format_rule_block_text(rule)
    assert text.startswith("タイトル名は『』で囲む\n")
    assert "例: PUBG→『PUBG』" in text
    assert "根拠: リリースA（https://prtimes.jp/a）" in text


def test_format_rule_block_text_minimal():
    assert format_rule_block_text({"rule": "ルールのみ"}) == "ルールのみ"


def test_extract_rule_statement_takes_first_line():
    block_text = "ルールA\n例: x\n根拠: y"
    assert extract_rule_statement(block_text) == "ルールA"


def test_ensure_checkbox_property_skips_when_exists():
    notion = MagicMock()
    notion.data_sources.retrieve.return_value = {
        "properties": {EXTRACTED_PROPERTY: {"type": "checkbox"}}
    }
    ensure_checkbox_property(notion)
    notion.data_sources.update.assert_not_called()


def test_ensure_checkbox_property_adds_when_missing():
    notion = MagicMock()
    notion.data_sources.retrieve.return_value = {"properties": {}}
    ensure_checkbox_property(notion)
    notion.data_sources.update.assert_called_once()
    _, kwargs = notion.data_sources.update.call_args
    assert kwargs["properties"][EXTRACTED_PROPERTY] == {"checkbox": {}}


def test_ensure_category_property_adds_when_missing():
    notion = MagicMock()
    notion.data_sources.retrieve.return_value = {"properties": {}}
    ensure_category_property(notion)
    _, kwargs = notion.data_sources.update.call_args
    options = kwargs["properties"][CATEGORY_PROPERTY]["select"]["options"]
    assert {"name": "タイトル名"} in options
    assert len(options) == 6


def test_ensure_category_property_skips_when_exists():
    notion = MagicMock()
    notion.data_sources.retrieve.return_value = {
        "properties": {CATEGORY_PROPERTY: {"type": "select"}}
    }
    ensure_category_property(notion)
    notion.data_sources.update.assert_not_called()
