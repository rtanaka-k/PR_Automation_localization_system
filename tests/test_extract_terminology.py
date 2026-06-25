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


def test_get_unprocessed_releases_paginates_and_filters():
    from extract_terminology import get_unprocessed_releases
    notion = MagicMock()
    page1 = {
        "id": "p1",
        "properties": {
            "タイトル（日本語）": {"title": [{"plain_text": "リリースA"}]},
            "PR Times URL": {"url": "https://prtimes.jp/a"},
        },
    }
    notion.data_sources.query.side_effect = [
        {"results": [page1], "has_more": True, "next_cursor": "c1"},
        {"results": [], "has_more": False, "next_cursor": None},
    ]
    releases = get_unprocessed_releases(notion)
    assert releases == [{"page_id": "p1", "title": "リリースA", "url": "https://prtimes.jp/a"}]
    assert notion.data_sources.query.call_count == 2
    _, kwargs = notion.data_sources.query.call_args_list[0]
    assert kwargs["filter"] == {"property": EXTRACTED_PROPERTY, "checkbox": {"equals": False}}


def test_get_unprocessed_releases_respects_max_count():
    from extract_terminology import get_unprocessed_releases
    notion = MagicMock()
    pages = [
        {
            "id": f"p{i}",
            "properties": {
                "タイトル（日本語）": {"title": [{"plain_text": f"リリース{i}"}]},
                "PR Times URL": {"url": f"https://prtimes.jp/{i}"},
            },
        }
        for i in range(5)
    ]
    notion.data_sources.query.return_value = {"results": pages, "has_more": False, "next_cursor": None}
    releases = get_unprocessed_releases(notion, max_count=2)
    assert len(releases) == 2


def test_fetch_bodies_for_batch_skips_empty_body(monkeypatch):
    import extract_terminology

    def fake_fetch(url):
        return "" if url == "https://prtimes.jp/empty" else f"本文 for {url}"

    monkeypatch.setattr(extract_terminology, "fetch_article_body", fake_fetch)
    releases = [
        {"page_id": "p1", "title": "A", "url": "https://prtimes.jp/ok"},
        {"page_id": "p2", "title": "B", "url": "https://prtimes.jp/empty"},
    ]
    result = extract_terminology.fetch_bodies_for_batch(releases)
    assert len(result) == 1
    assert result[0]["page_id"] == "p1"
    assert result[0]["body"] == "本文 for https://prtimes.jp/ok"
