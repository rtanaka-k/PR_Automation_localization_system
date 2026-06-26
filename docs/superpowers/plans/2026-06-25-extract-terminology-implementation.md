# 用語・表記ルール抽出機能 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `extract_terminology.py` を新規作成し、Notion「ニュースリリースDB」に取り込み済みのPR Times原稿371件から固有名詞の用語と文体・表記ルールをClaude APIで抽出し、用語集DBへのpending登録と「KRAFTON Japan 表記ルール」ページへの追記を行う。差分再実行に対応する。

**Architecture:** `prtimes_to_notion.py`と同じ単体スクリプト構成。未処理release取得→本文再取得→10件バッチでClaude APIに1回投げて用語+表記ルールを同時抽出→用語は即時pending登録、表記ルールはメモリに蓄積→全バッチ完了後に表記ルール候補をClaude APIで統合→Notionページに追記。各関数は純粋ロジック（バッチ分割・重複排除・JSON解析・テキスト整形）とI/O（Notion/Claude呼び出し）に分離し、純粋ロジックはモック不要でテストする。

**Tech Stack:** Python, notion-client 3.1.0（`data_sources.*` API）, anthropic SDK（`claude-sonnet-4-6`）, pytest（新規導入）, requests + BeautifulSoup（`prtimes_to_notion.fetch_article_body`を再利用）

## Global Constraints

- Notion DB ID: ニュースリリースDB `database_id=7606be38ff494d1f84902f8f82ebfee0` / `data_source_id=68f52f52-f7dc-4ab4-9941-61c7c7d7f6e7`
- Notion DB ID: 用語集DB `database_id=8959e88bcfe34936a59cc82232215266` / `data_source_id=0d2447b5-6fcf-414b-a3b4-88dad64f6b8b`
- notion-client 3.1.0 はクエリ・スキーマ変更系が `data_sources.query` / `data_sources.retrieve` / `data_sources.update` に完全移行済み（`databases.query`等は存在しない）。`pages.create`/`pages.update`のparentには`database_id`を使う（後方互換）
- Claude モデルは既存コードと同じ `claude-sonnet-4-6` を使う
- 既存のNotionプロパティ名（日本語）は変更しない: 「タイトル（日本語）」「PR Times URL」「日本語表記」「ステータス」「EN表記」「抽出元リリース」
- 新規プロパティ: ニュースリリースDBに「用語抽出済み」(checkbox)、用語集DBに「カテゴリ」(select, 候補: タイトル名/キャラクター/ゲームシステム/イベント・大会/組織・役職/その他) を、存在しない場合のみ自動追加する
- 新規Notionページ「KRAFTON Japan 表記ルール」はワークスペース直下に作成する（ユーザー確認済み）。見出しは「要確認」「承認済み」の2つ
- CLIオプションは`prtimes_to_notion.py`と同じ流儀: `--dry-run`, `--max N`（0=全件）, `--batch-size N`（デフォルト10）
- 環境変数: `NOTION_TOKEN`, `ANTHROPIC_KEY`（Streamlit Cloud Secretsではなく環境変数のみ。`prtimes_to_notion.py`と同方式）
- エラー処理: 本文取得失敗→該当releaseをスキップして継続、Claude APIエラー→バッチ単位で1回リトライ後スキップ（用語抽出済みはONにしない）、用語のNotion書き込みエラー→該当用語のみスキップ
- 重複排除はすべて完全一致チェック（名寄せロジックは作らない）

---

### Task 1: プロジェクトセットアップ + 純粋ロジック関数（バッチ分割・重複排除・ルール整形）

**Files:**
- Modify: `requirements.txt`
- Create: `conftest.py`（リポジトリルート、空ファイル。pytestがルートを`sys.path`に追加するための標準的手法）
- Create: `extract_terminology.py`（このタスクではモジュール先頭部分と純粋関数のみ）
- Test: `tests/test_extract_terminology.py`

**Interfaces:**
- Produces: `build_batches(releases: list[dict], batch_size: int) -> list[list[dict]]`
- Produces: `dedupe_new_terms(candidate_terms: list[dict], known_ja: set[str]) -> list[dict]`（`known_ja`を破壊的に更新する）
- Produces: `format_rule_block_text(rule: dict) -> str`
- Produces: `extract_rule_statement(block_text: str) -> str`

- [ ] **Step 1: requirements.txtにpytestを追加**

`requirements.txt`の末尾に追記:
```
pytest>=8.0.0
```

- [ ] **Step 2: conftest.pyを作成**

リポジトリルートに `conftest.py` を作成（pytestがリポジトリルートを`sys.path`に追加し、`tests/`配下から`import extract_terminology`できるようにするため）:
```python
# pytestがリポジトリルートをsys.pathに含めるためのマーカーファイル
```

- [ ] **Step 3: pytestをインストール**

```bash
pip install -r requirements.txt
```

- [ ] **Step 4: extract_terminology.pyの先頭部分を作成**

`extract_terminology.py`を新規作成:
```python
"""
KRAFTON Japan 用語・表記ルール抽出スクリプト
==============================================
Notion「ニュースリリースDB」に取り込み済みのPR Times原稿から、固有名詞の用語と
文体・表記ルールをClaude APIで抽出し、用語集DBへのpending登録および
「KRAFTON Japan 表記ルール」ページへの追記を行う。

使い方:
  1. 依存パッケージをインストール
     pip install -r requirements.txt

  2. 環境変数を設定
     Windows PowerShell:
       $env:NOTION_TOKEN = "ntn_xxxxxxxxxxxx"
       $env:ANTHROPIC_KEY = "sk-ant-xxxxxxxxxxxx"

  3. 実行
     python extract_terminology.py

     オプション:
       --max 50          処理するリリース件数の上限（デフォルト: 全件）
       --batch-size 10   Claude APIに渡す1バッチあたりのリリース件数（デフォルト: 10）
       --dry-run         Notionへの書き込みをせず内容をプレビュー
"""

import os
import sys
import re
import json
import argparse
from notion_client import Client
from anthropic import Anthropic

from prtimes_to_notion import fetch_article_body

# ============================================================
# 設定
# ============================================================
NOTION_RELEASE_DB_ID = "7606be38ff494d1f84902f8f82ebfee0"
NOTION_RELEASE_DATA_SOURCE_ID = "68f52f52-f7dc-4ab4-9941-61c7c7d7f6e7"
NOTION_GLOSSARY_DB_ID = "8959e88bcfe34936a59cc82232215266"
NOTION_GLOSSARY_DATA_SOURCE_ID = "0d2447b5-6fcf-414b-a3b4-88dad64f6b8b"

CLAUDE_MODEL = "claude-sonnet-4-6"

EXTRACTED_PROPERTY = "用語抽出済み"
CATEGORY_PROPERTY = "カテゴリ"
TERM_CATEGORY_OPTIONS = ["タイトル名", "キャラクター", "ゲームシステム", "イベント・大会", "組織・役職", "その他"]

STYLE_RULES_PAGE_TITLE = "KRAFTON Japan 表記ルール"
CONFIRM_HEADING = "要確認"
APPROVED_HEADING = "承認済み"


# ============================================================
# 純粋ロジック関数（Notion/Claude呼び出しを含まない）
# ============================================================

def build_batches(releases: list[dict], batch_size: int) -> list[list[dict]]:
    """releaseのリストをbatch_size件ずつに分割する"""
    return [releases[i:i + batch_size] for i in range(0, len(releases), batch_size)]


def dedupe_new_terms(candidate_terms: list[dict], known_ja: set[str]) -> list[dict]:
    """既知の日本語表記(known_ja)と重複しない用語だけを返し、known_jaに追加する"""
    accepted = []
    for term in candidate_terms:
        ja = term.get("ja", "").strip()
        if not ja or ja in known_ja:
            continue
        accepted.append(term)
        known_ja.add(ja)
    return accepted


def format_rule_block_text(rule: dict) -> str:
    """表記ルール1件をNotionの箇条書きブロック用テキストに整形する。
    1行目がルール文そのものになるようにし、extract_rule_statement()で復元できるようにする。
    """
    lines = [rule["rule"]]
    if rule.get("example"):
        lines.append(f"例: {rule['example']}")
    sources = rule.get("sources", [])
    if sources:
        src_text = "、".join(f"{s['title']}（{s['url']}）" for s in sources[:3])
        lines.append(f"根拠: {src_text}")
    return "\n".join(lines)


def extract_rule_statement(block_text: str) -> str:
    """format_rule_block_textで整形したテキストからルール文（1行目）だけを取り出す"""
    return block_text.split("\n", 1)[0]
```

- [ ] **Step 5: 純粋ロジック関数のテストを書く**

`tests/test_extract_terminology.py`を新規作成:
```python
from extract_terminology import (
    build_batches,
    dedupe_new_terms,
    format_rule_block_text,
    extract_rule_statement,
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
```

- [ ] **Step 6: テストを実行して全件成功することを確認**

```bash
cd "C:\Users\r.tanaka\Documents\Claude Cowork\Projects\PR_Automation"
python -m pytest tests/test_extract_terminology.py -v
```
Expected: 7 passed

- [ ] **Step 7: コミット**

```bash
git add requirements.txt conftest.py extract_terminology.py tests/test_extract_terminology.py
git commit -m "feat: 用語抽出スクリプトの土台と純粋ロジック関数を追加"
```

---

### Task 2: Notionスキーマ自動追加（「用語抽出済み」「カテゴリ」プロパティ）

**Files:**
- Modify: `extract_terminology.py`
- Test: `tests/test_extract_terminology.py`

**Interfaces:**
- Consumes: `EXTRACTED_PROPERTY`, `CATEGORY_PROPERTY`, `TERM_CATEGORY_OPTIONS`, `NOTION_RELEASE_DATA_SOURCE_ID`, `NOTION_GLOSSARY_DATA_SOURCE_ID`（Task 1で定義済み）
- Produces: `ensure_checkbox_property(notion: Client) -> None`
- Produces: `ensure_category_property(notion: Client) -> None`

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_extract_terminology.py`に追記:
```python
from unittest.mock import MagicMock
from extract_terminology import (
    ensure_checkbox_property,
    ensure_category_property,
    EXTRACTED_PROPERTY,
    CATEGORY_PROPERTY,
)


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
```

- [ ] **Step 2: テストを実行して失敗を確認**

```bash
python -m pytest tests/test_extract_terminology.py -v -k ensure
```
Expected: FAIL with `ImportError: cannot import name 'ensure_checkbox_property'`

- [ ] **Step 3: 実装を追加**

`extract_terminology.py`の純粋ロジック関数群の後に追記:
```python
# ============================================================
# Notionスキーマ管理
# ============================================================

def ensure_checkbox_property(notion: Client) -> None:
    """ニュースリリースDBに「用語抽出済み」チェックボックスがなければ追加する"""
    ds = notion.data_sources.retrieve(data_source_id=NOTION_RELEASE_DATA_SOURCE_ID)
    if EXTRACTED_PROPERTY in ds.get("properties", {}):
        return
    notion.data_sources.update(
        data_source_id=NOTION_RELEASE_DATA_SOURCE_ID,
        properties={EXTRACTED_PROPERTY: {"checkbox": {}}},
    )


def ensure_category_property(notion: Client) -> None:
    """用語集DBに「カテゴリ」selectプロパティがなければ追加する"""
    ds = notion.data_sources.retrieve(data_source_id=NOTION_GLOSSARY_DATA_SOURCE_ID)
    if CATEGORY_PROPERTY in ds.get("properties", {}):
        return
    notion.data_sources.update(
        data_source_id=NOTION_GLOSSARY_DATA_SOURCE_ID,
        properties={
            CATEGORY_PROPERTY: {
                "select": {"options": [{"name": c} for c in TERM_CATEGORY_OPTIONS]}
            }
        },
    )
```

- [ ] **Step 4: テストを実行して成功を確認**

```bash
python -m pytest tests/test_extract_terminology.py -v -k ensure
```
Expected: 4 passed

- [ ] **Step 5: コミット**

```bash
git add extract_terminology.py tests/test_extract_terminology.py
git commit -m "feat: 用語抽出に必要なNotionスキーマの自動追加を実装"
```

---

### Task 3: 未処理リリースの取得 + 本文再取得

**Files:**
- Modify: `extract_terminology.py`
- Test: `tests/test_extract_terminology.py`

**Interfaces:**
- Consumes: `fetch_article_body(url: str) -> str`（`prtimes_to_notion`からimport済み、Task 1）
- Produces: `get_unprocessed_releases(notion: Client, max_count: int = 0) -> list[dict]`（各要素は`{"page_id": str, "title": str, "url": str}`）
- Produces: `fetch_bodies_for_batch(releases: list[dict]) -> list[dict]`（各要素は入力dict + `"body": str`、本文取得失敗分は除外）

- [ ] **Step 1: 失敗するテストを書く**

```python
def test_get_unprocessed_releases_paginates_and_filters():
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
```

- [ ] **Step 2: テストを実行して失敗を確認**

```bash
python -m pytest tests/test_extract_terminology.py -v -k "unprocessed or fetch_bodies"
```
Expected: FAIL with `ImportError`

- [ ] **Step 3: 実装を追加**

```python
# ============================================================
# リリース取得
# ============================================================

def get_unprocessed_releases(notion: Client, max_count: int = 0) -> list[dict]:
    """「用語抽出済み」が未チェックのreleaseをニュースリリースDBから取得する"""
    releases = []
    cursor = None
    while True:
        params = {
            "data_source_id": NOTION_RELEASE_DATA_SOURCE_ID,
            "page_size": 100,
            "filter": {"property": EXTRACTED_PROPERTY, "checkbox": {"equals": False}},
        }
        if cursor:
            params["start_cursor"] = cursor
        res = notion.data_sources.query(**params)
        for page in res["results"]:
            props = page["properties"]
            title_items = props.get("タイトル（日本語）", {}).get("title", [])
            title = "".join(t.get("plain_text", "") for t in title_items)
            url = props.get("PR Times URL", {}).get("url") or ""
            releases.append({"page_id": page["id"], "title": title, "url": url})
            if max_count and len(releases) >= max_count:
                return releases
        if not res.get("has_more"):
            break
        cursor = res["next_cursor"]
    return releases


def fetch_bodies_for_batch(releases: list[dict]) -> list[dict]:
    """各releaseのPR Times URLから本文を再取得する。取得失敗分はログを出して除外する"""
    result = []
    for release in releases:
        body = fetch_article_body(release["url"]) if release["url"] else ""
        if not body:
            print(f"  ⚠️  本文取得失敗のためスキップ: {release['title']}")
            continue
        result.append({**release, "body": body})
    return result
```

- [ ] **Step 4: テストを実行して成功を確認**

```bash
python -m pytest tests/test_extract_terminology.py -v -k "unprocessed or fetch_bodies"
```
Expected: 3 passed

- [ ] **Step 5: コミット**

```bash
git add extract_terminology.py tests/test_extract_terminology.py
git commit -m "feat: 未処理リリースの取得と本文再取得を実装"
```

---

### Task 4: 用語集の既存用語取得 + pending登録

**Files:**
- Modify: `extract_terminology.py`
- Test: `tests/test_extract_terminology.py`

**Interfaces:**
- Produces: `get_existing_term_set(notion: Client) -> set[str]`
- Produces: `save_pending_term(notion: Client, term: dict, source_url: str) -> None`

- [ ] **Step 1: 失敗するテストを書く**

```python
def test_get_existing_term_set_collects_titles():
    notion = MagicMock()
    notion.data_sources.query.return_value = {
        "results": [
            {"properties": {"日本語表記": {"title": [{"plain_text": "クラフトン"}]}}},
            {"properties": {"日本語表記": {"title": [{"plain_text": "インゾイ"}]}}},
        ],
        "has_more": False,
        "next_cursor": None,
    }
    assert get_existing_term_set(notion) == {"クラフトン", "インゾイ"}


def test_save_pending_term_includes_category_and_source():
    notion = MagicMock()
    term = {"ja": "インゾイ", "en": "inZOI", "category": "タイトル名"}
    save_pending_term(notion, term, "https://prtimes.jp/x")
    _, kwargs = notion.pages.create.call_args
    props = kwargs["properties"]
    assert props["日本語表記"]["title"][0]["text"]["content"] == "インゾイ"
    assert props["EN表記"]["rich_text"][0]["text"]["content"] == "inZOI"
    assert props["カテゴリ"]["select"]["name"] == "タイトル名"
    assert props["抽出元リリース"]["url"] == "https://prtimes.jp/x"
    assert props["ステータス"]["select"]["name"] == "pending"


def test_save_pending_term_omits_optional_fields_when_absent():
    notion = MagicMock()
    save_pending_term(notion, {"ja": "X"}, "")
    _, kwargs = notion.pages.create.call_args
    props = kwargs["properties"]
    assert "カテゴリ" not in props
    assert "抽出元リリース" not in props
    assert "EN表記" not in props
```

- [ ] **Step 2: テストを実行して失敗を確認**

```bash
python -m pytest tests/test_extract_terminology.py -v -k "existing_term_set or save_pending_term"
```
Expected: FAIL with `ImportError`

- [ ] **Step 3: 実装を追加**

```python
# ============================================================
# 用語集DB
# ============================================================

def get_existing_term_set(notion: Client) -> set[str]:
    """用語集DBの既存エントリ（pending＋approved全件）の日本語表記集合を取得する"""
    known = set()
    cursor = None
    while True:
        params = {"data_source_id": NOTION_GLOSSARY_DATA_SOURCE_ID, "page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        res = notion.data_sources.query(**params)
        for page in res["results"]:
            title_items = page["properties"].get("日本語表記", {}).get("title", [])
            ja = "".join(t.get("plain_text", "") for t in title_items)
            if ja:
                known.add(ja)
        if not res.get("has_more"):
            break
        cursor = res["next_cursor"]
    return known


def save_pending_term(notion: Client, term: dict, source_url: str) -> None:
    """新出語句をpendingステータスで用語集DBに登録する"""
    props = {
        "日本語表記": {"title": [{"text": {"content": term["ja"]}}]},
        "ステータス": {"select": {"name": "pending"}},
    }
    if term.get("en"):
        props["EN表記"] = {"rich_text": [{"text": {"content": term["en"]}}]}
    if term.get("category"):
        props[CATEGORY_PROPERTY] = {"select": {"name": term["category"]}}
    if source_url:
        props["抽出元リリース"] = {"url": source_url}
    notion.pages.create(parent={"database_id": NOTION_GLOSSARY_DB_ID}, properties=props)
```

- [ ] **Step 4: テストを実行して成功を確認**

```bash
python -m pytest tests/test_extract_terminology.py -v -k "existing_term_set or save_pending_term"
```
Expected: 3 passed

- [ ] **Step 5: コミット**

```bash
git add extract_terminology.py tests/test_extract_terminology.py
git commit -m "feat: 用語集DBの既存用語取得とpending登録を実装"
```

---

### Task 5: Claude APIによる用語・表記ルール抽出

**Files:**
- Modify: `extract_terminology.py`
- Test: `tests/test_extract_terminology.py`

**Interfaces:**
- Produces: `parse_extraction_response(raw_text: str) -> dict`（常に`{"terms": [...], "style_rules": [...]}`形式を返す）
- Produces: `extract_terms_and_style(text_ja: str, text_kr: str = "", text_en: str = "", api_key: str = "") -> dict`

- [ ] **Step 1: 失敗するテストを書く**

```python
def test_parse_extraction_response_parses_clean_json():
    raw = '{"terms": [{"ja": "A"}], "style_rules": [{"rule": "R"}]}'
    result = parse_extraction_response(raw)
    assert result == {"terms": [{"ja": "A"}], "style_rules": [{"rule": "R"}]}


def test_parse_extraction_response_strips_surrounding_text():
    raw = 'はい、結果はこちらです:\n{"terms": [], "style_rules": []}\nご確認ください。'
    assert parse_extraction_response(raw) == {"terms": [], "style_rules": []}


def test_parse_extraction_response_returns_empty_on_invalid_json():
    assert parse_extraction_response("不正な応答です") == {"terms": [], "style_rules": []}


def test_parse_extraction_response_defaults_missing_keys():
    assert parse_extraction_response('{"terms": [{"ja": "A"}]}') == {
        "terms": [{"ja": "A"}],
        "style_rules": [],
    }


def test_extract_terms_and_style_calls_claude_and_parses(monkeypatch):
    import extract_terminology

    fake_response = MagicMock()
    fake_response.content = [MagicMock(text='{"terms": [{"ja": "X"}], "style_rules": []}')]
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_response
    monkeypatch.setattr(extract_terminology, "Anthropic", lambda api_key: fake_client)

    result = extract_terminology.extract_terms_and_style("本文テキスト", api_key="dummy")
    assert result == {"terms": [{"ja": "X"}], "style_rules": []}
    fake_client.messages.create.assert_called_once()
    _, kwargs = fake_client.messages.create.call_args
    assert kwargs["model"] == extract_terminology.CLAUDE_MODEL
```

- [ ] **Step 2: テストを実行して失敗を確認**

```bash
python -m pytest tests/test_extract_terminology.py -v -k "parse_extraction or extract_terms_and_style"
```
Expected: FAIL with `ImportError`

- [ ] **Step 3: 実装を追加**

```python
# ============================================================
# Claude API: 用語・表記ルール抽出
# ============================================================

def parse_extraction_response(raw_text: str) -> dict:
    """Claude応答から{terms, style_rules}のJSONを頑健に抽出する"""
    match = re.search(r"\{.*\}", raw_text, re.DOTALL)
    if not match:
        return {"terms": [], "style_rules": []}
    try:
        data = json.loads(match.group())
    except Exception:
        return {"terms": [], "style_rules": []}
    return {
        "terms": data.get("terms") or [],
        "style_rules": data.get("style_rules") or [],
    }


def extract_terms_and_style(text_ja: str, text_kr: str = "", text_en: str = "",
                             api_key: str = "") -> dict:
    """release本文（複数件を結合したテキスト）から用語・表記ルール候補を抽出する。
    将来KR/EN原文付きで呼び出せるよう汎用シグネチャにしているが、今回はtext_jaのみ使用する。
    """
    client = Anthropic(api_key=api_key)
    prompt = f"""以下は複数のプレスリリース本文です。それぞれから固有名詞の用語と文体・表記ルールを抽出してください。

【本文】
{text_ja}

【出力形式】JSON1個のみ（説明不要）
{{
  "terms": [{{"ja": "日本語表記", "en": "英語表記（なければ空文字）", "category": "タイトル名|キャラクター|ゲームシステム|イベント・大会|組織・役職|その他"}}],
  "style_rules": [{{"rule": "ルールの説明", "example": "Before→After、または本文からの引用例"}}]
}}
該当がなければそれぞれ空配列を返す。"""

    res = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    return parse_extraction_response(res.content[0].text)
```

- [ ] **Step 4: テストを実行して成功を確認**

```bash
python -m pytest tests/test_extract_terminology.py -v -k "parse_extraction or extract_terms_and_style"
```
Expected: 5 passed

- [ ] **Step 5: コミット**

```bash
git add extract_terminology.py tests/test_extract_terminology.py
git commit -m "feat: Claude APIによる用語・表記ルール抽出を実装"
```

---

### Task 6: 表記ルール候補の統合（Claude API 2回目呼び出し）

**Files:**
- Modify: `extract_terminology.py`
- Test: `tests/test_extract_terminology.py`

**Interfaces:**
- Consumes: 候補要素の形式 `{"rule": str, "example": str, "source_title": str, "source_url": str}`
- Produces: `build_consolidation_prompt(candidates: list[dict]) -> str`
- Produces: `parse_consolidation_response(raw_text: str) -> list[dict]`
- Produces: `consolidate_style_rules(candidates: list[dict], api_key: str) -> list[dict]`（戻り値の各要素は`{"rule": str, "example": str, "sources": [{"title": str, "url": str}, ...]}`）

- [ ] **Step 1: 失敗するテストを書く**

```python
def test_build_consolidation_prompt_lists_candidates_with_indices():
    candidates = [
        {"rule": "ルールA", "example": "x→y", "source_title": "T1", "source_url": "u1"},
        {"rule": "ルールB", "example": "", "source_title": "T2", "source_url": "u2"},
    ]
    prompt = build_consolidation_prompt(candidates)
    assert "0. ルールA（例: x→y）" in prompt
    assert "1. ルールB" in prompt


def test_parse_consolidation_response_parses_array():
    raw = '[{"rule": "R", "source_indices": [0, 1]}]'
    assert parse_consolidation_response(raw) == [{"rule": "R", "source_indices": [0, 1]}]


def test_parse_consolidation_response_invalid_returns_empty():
    assert parse_consolidation_response("no json here") == []


def test_consolidate_style_rules_maps_indices_to_sources(monkeypatch):
    import extract_terminology

    candidates = [
        {"rule": "ルールA", "example": "", "source_title": "T1", "source_url": "u1"},
        {"rule": "ルールA重複", "example": "", "source_title": "T2", "source_url": "u2"},
    ]
    fake_response = MagicMock()
    fake_response.content = [
        MagicMock(text='[{"rule": "統合ルールA", "example": "", "source_indices": [0, 1]}]')
    ]
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_response
    monkeypatch.setattr(extract_terminology, "Anthropic", lambda api_key: fake_client)

    result = extract_terminology.consolidate_style_rules(candidates, api_key="dummy")
    assert result == [{
        "rule": "統合ルールA",
        "example": "",
        "sources": [{"title": "T1", "url": "u1"}, {"title": "T2", "url": "u2"}],
    }]


def test_consolidate_style_rules_returns_empty_for_no_candidates():
    import extract_terminology
    assert extract_terminology.consolidate_style_rules([], api_key="dummy") == []
```

- [ ] **Step 2: テストを実行して失敗を確認**

```bash
python -m pytest tests/test_extract_terminology.py -v -k consolidat
```
Expected: FAIL with `ImportError`

- [ ] **Step 3: 実装を追加**

```python
# ============================================================
# Claude API: 表記ルール統合
# ============================================================

def build_consolidation_prompt(candidates: list[dict]) -> str:
    lines = []
    for i, c in enumerate(candidates):
        example = c.get("example", "")
        suffix = f"（例: {example}）" if example else ""
        lines.append(f"{i}. {c['rule']}{suffix}")
    listing = "\n".join(lines)
    return f"""以下は複数のプレスリリースから抽出された表記ルール候補です。類似・重複する候補を統合し、一意で明確な最終リストにしてください。

【候補一覧】
{listing}

【出力形式】JSON配列のみ（説明不要）
[
  {{"rule": "統合後のルール説明", "example": "代表的な具体例（任意）", "source_indices": [統合元の番号のリスト]}}
]"""


def parse_consolidation_response(raw_text: str) -> list[dict]:
    match = re.search(r"\[.*\]", raw_text, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group())
    except Exception:
        return []
    return data if isinstance(data, list) else []


def consolidate_style_rules(candidates: list[dict], api_key: str) -> list[dict]:
    """蓄積した表記ルール候補をClaude APIで統合し、根拠リンク付きの最終リストを返す"""
    if not candidates:
        return []

    client = Anthropic(api_key=api_key)
    prompt = build_consolidation_prompt(candidates)
    res = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    consolidated = parse_consolidation_response(res.content[0].text)

    rules = []
    for item in consolidated:
        sources = []
        for idx in item.get("source_indices", []):
            if 0 <= idx < len(candidates):
                cand = candidates[idx]
                sources.append({"title": cand["source_title"], "url": cand["source_url"]})
        rules.append({
            "rule": item.get("rule", ""),
            "example": item.get("example", ""),
            "sources": sources,
        })
    return rules
```

- [ ] **Step 4: テストを実行して成功を確認**

```bash
python -m pytest tests/test_extract_terminology.py -v -k consolidat
```
Expected: 5 passed

- [ ] **Step 5: コミット**

```bash
git add extract_terminology.py tests/test_extract_terminology.py
git commit -m "feat: 表記ルール候補の統合処理を実装"
```

---

### Task 7: 「KRAFTON Japan 表記ルール」ページの作成・取得・追記

**Files:**
- Modify: `extract_terminology.py`
- Test: `tests/test_extract_terminology.py`

**Interfaces:**
- Consumes: `format_rule_block_text`, `extract_rule_statement`（Task 1）, `STYLE_RULES_PAGE_TITLE`, `CONFIRM_HEADING`, `APPROVED_HEADING`
- Produces: `find_style_rules_page(notion: Client) -> str | None`
- Produces: `find_heading_block_id(notion: Client, page_id: str, heading_text: str) -> str | None`
- Produces: `create_style_rules_page(notion: Client) -> tuple[str, str]`（`(page_id, confirm_heading_block_id)`）
- Produces: `get_or_create_style_rules_page(notion: Client) -> tuple[str, str]`
- Produces: `get_existing_rule_texts(notion: Client, page_id: str) -> set[str]`
- Produces: `append_style_rules_to_page(notion: Client, page_id: str, confirm_heading_id: str, rules: list[dict], existing_texts: set[str]) -> int`（戻り値は実際に追記した件数）

- [ ] **Step 1: 失敗するテストを書く**

```python
def test_find_style_rules_page_matches_exact_title():
    notion = MagicMock()
    notion.search.return_value = {
        "results": [
            {"id": "page1", "properties": {"title": {"title": [{"plain_text": "KRAFTON Japan 表記ルール"}]}}},
        ]
    }
    assert find_style_rules_page(notion) == "page1"


def test_find_style_rules_page_returns_none_when_not_found():
    notion = MagicMock()
    notion.search.return_value = {"results": []}
    assert find_style_rules_page(notion) is None


def test_find_heading_block_id_matches_heading_2():
    notion = MagicMock()
    notion.blocks.children.list.return_value = {
        "results": [
            {"id": "h1", "type": "heading_2", "heading_2": {"rich_text": [{"plain_text": "要確認"}]}},
            {"id": "h2", "type": "heading_2", "heading_2": {"rich_text": [{"plain_text": "承認済み"}]}},
        ],
        "has_more": False,
        "next_cursor": None,
    }
    assert find_heading_block_id(notion, "page1", "要確認") == "h1"


def test_create_style_rules_page_creates_headings_and_returns_ids():
    notion = MagicMock()
    notion.pages.create.return_value = {"id": "newpage"}
    notion.blocks.children.append.return_value = {"results": [{"id": "heading1"}, {"id": "heading2"}]}
    page_id, confirm_id = create_style_rules_page(notion)
    assert page_id == "newpage"
    assert confirm_id == "heading1"
    _, kwargs = notion.pages.create.call_args
    assert kwargs["parent"] == {"type": "workspace", "workspace": True}


def test_get_existing_rule_texts_extracts_first_line_only():
    notion = MagicMock()
    notion.blocks.children.list.return_value = {
        "results": [
            {"type": "bulleted_list_item", "bulleted_list_item": {
                "rich_text": [{"plain_text": "ルールA\n例: x\n根拠: y"}]}},
            {"type": "heading_2", "heading_2": {"rich_text": [{"plain_text": "要確認"}]}},
        ],
        "has_more": False,
        "next_cursor": None,
    }
    assert get_existing_rule_texts(notion, "page1") == {"ルールA"}


def test_append_style_rules_to_page_skips_duplicates_and_uses_after():
    notion = MagicMock()
    rules = [{"rule": "既存ルール"}, {"rule": "新規ルール", "example": "", "sources": []}]
    count = append_style_rules_to_page(notion, "page1", "heading1", rules, {"既存ルール"})
    assert count == 1
    _, kwargs = notion.blocks.children.append.call_args
    assert kwargs["after"] == "heading1"
    assert len(kwargs["children"]) == 1


def test_append_style_rules_to_page_no_new_rules_skips_api_call():
    notion = MagicMock()
    count = append_style_rules_to_page(notion, "page1", "heading1", [{"rule": "既存"}], {"既存"})
    assert count == 0
    notion.blocks.children.append.assert_not_called()
```

- [ ] **Step 2: テストを実行して失敗を確認**

```bash
python -m pytest tests/test_extract_terminology.py -v -k "style_rules_page or heading_block or existing_rule_texts or append_style_rules"
```
Expected: FAIL with `ImportError`

- [ ] **Step 3: 実装を追加**

```python
# ============================================================
# 「KRAFTON Japan 表記ルール」ページ
# ============================================================

def find_style_rules_page(notion: Client) -> str | None:
    """ワークスペース内から「KRAFTON Japan 表記ルール」ページを完全一致で検索する"""
    res = notion.search(query=STYLE_RULES_PAGE_TITLE, filter={"property": "object", "value": "page"})
    for page in res.get("results", []):
        title_items = page.get("properties", {}).get("title", {}).get("title", [])
        title_text = "".join(t.get("plain_text", "") for t in title_items)
        if title_text == STYLE_RULES_PAGE_TITLE:
            return page["id"]
    return None


def find_heading_block_id(notion: Client, page_id: str, heading_text: str) -> str | None:
    """ページ内のheading_2ブロックをテキスト完全一致で検索する"""
    cursor = None
    while True:
        params = {"block_id": page_id, "page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        res = notion.blocks.children.list(**params)
        for block in res["results"]:
            if block["type"] == "heading_2":
                text_items = block["heading_2"].get("rich_text", [])
                text = "".join(t.get("plain_text", "") for t in text_items)
                if text == heading_text:
                    return block["id"]
        if not res.get("has_more"):
            break
        cursor = res["next_cursor"]
    return None


def create_style_rules_page(notion: Client) -> tuple[str, str]:
    """「KRAFTON Japan 表記ルール」ページをワークスペース直下に新規作成する"""
    page = notion.pages.create(
        parent={"type": "workspace", "workspace": True},
        properties={"title": {"title": [{"text": {"content": STYLE_RULES_PAGE_TITLE}}]}},
    )
    page_id = page["id"]
    res = notion.blocks.children.append(
        block_id=page_id,
        children=[
            {"object": "block", "type": "heading_2",
             "heading_2": {"rich_text": [{"text": {"content": CONFIRM_HEADING}}]}},
            {"object": "block", "type": "heading_2",
             "heading_2": {"rich_text": [{"text": {"content": APPROVED_HEADING}}]}},
        ],
    )
    confirm_heading_id = res["results"][0]["id"]
    return page_id, confirm_heading_id


def get_or_create_style_rules_page(notion: Client) -> tuple[str, str]:
    page_id = find_style_rules_page(notion)
    if page_id:
        confirm_id = find_heading_block_id(notion, page_id, CONFIRM_HEADING)
        return page_id, confirm_id
    return create_style_rules_page(notion)


def get_existing_rule_texts(notion: Client, page_id: str) -> set[str]:
    """ページ内の既存箇条書きからルール文（1行目）の集合を取得する（要確認・承認済み両方）"""
    texts = set()
    cursor = None
    while True:
        params = {"block_id": page_id, "page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        res = notion.blocks.children.list(**params)
        for block in res["results"]:
            if block["type"] == "bulleted_list_item":
                items = block["bulleted_list_item"].get("rich_text", [])
                full_text = "".join(t.get("plain_text", "") for t in items)
                texts.add(extract_rule_statement(full_text))
        if not res.get("has_more"):
            break
        cursor = res["next_cursor"]
    return texts


def append_style_rules_to_page(notion: Client, page_id: str, confirm_heading_id: str,
                                 rules: list[dict], existing_texts: set[str]) -> int:
    """重複しない表記ルールを「要確認」見出しの直後にまとめて追記する"""
    new_blocks = []
    for rule in rules:
        if rule["rule"] in existing_texts:
            continue
        block_text = format_rule_block_text(rule)
        new_blocks.append({
            "object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": [{"text": {"content": block_text}}]},
        })
    if not new_blocks:
        return 0
    notion.blocks.children.append(block_id=page_id, children=new_blocks, after=confirm_heading_id)
    return len(new_blocks)
```

- [ ] **Step 4: テストを実行して成功を確認**

```bash
python -m pytest tests/test_extract_terminology.py -v -k "style_rules_page or heading_block or existing_rule_texts or append_style_rules"
```
Expected: 7 passed

- [ ] **Step 5: コミット**

```bash
git add extract_terminology.py tests/test_extract_terminology.py
git commit -m "feat: 表記ルールページの作成・取得・追記処理を実装"
```

---

### Task 8: バッチ処理オーケストレーション + CLI（main関数）

**Files:**
- Modify: `extract_terminology.py`
- Test: `tests/test_extract_terminology.py`

**Interfaces:**
- Consumes: Task 1〜7で定義した全関数
- Produces: `process_batch(batch: list[dict], known_ja: set[str], style_candidates: list[dict], notion: Client, api_key: str, dry_run: bool) -> bool`
- Produces: `main() -> None`

- [ ] **Step 1: 失敗するテストを書く（process_batch）**

```python
def test_process_batch_retries_once_then_skips_on_repeated_failure(monkeypatch):
    import extract_terminology
    monkeypatch.setattr(
        extract_terminology, "extract_terms_and_style",
        MagicMock(side_effect=[Exception("fail1"), Exception("fail2")]),
    )
    notion = MagicMock()
    batch = [{"page_id": "p1", "title": "T", "url": "u1", "body": "本文"}]
    ok = extract_terminology.process_batch(batch, set(), [], notion, "key", dry_run=False)
    assert ok is False
    notion.pages.update.assert_not_called()


def test_process_batch_succeeds_and_marks_checkbox(monkeypatch):
    import extract_terminology
    fake_extract = MagicMock(return_value={
        "terms": [{"ja": "新語", "category": "タイトル名"}],
        "style_rules": [{"rule": "ルールX", "example": ""}],
    })
    monkeypatch.setattr(extract_terminology, "extract_terms_and_style", fake_extract)
    monkeypatch.setattr(extract_terminology, "save_pending_term", MagicMock())
    notion = MagicMock()
    batch = [{"page_id": "p1", "title": "T", "url": "u1", "body": "本文"}]
    known_ja = set()
    style_candidates = []
    ok = extract_terminology.process_batch(batch, known_ja, style_candidates, notion, "key", dry_run=False)
    assert ok is True
    assert "新語" in known_ja
    assert style_candidates == [{"rule": "ルールX", "example": "", "source_title": "T", "source_url": "u1"}]
    notion.pages.update.assert_called_once_with(
        page_id="p1", properties={extract_terminology.EXTRACTED_PROPERTY: {"checkbox": True}},
    )


def test_process_batch_dry_run_skips_writes(monkeypatch):
    import extract_terminology
    fake_extract = MagicMock(return_value={"terms": [{"ja": "新語"}], "style_rules": []})
    monkeypatch.setattr(extract_terminology, "extract_terms_and_style", fake_extract)
    save_mock = MagicMock()
    monkeypatch.setattr(extract_terminology, "save_pending_term", save_mock)
    notion = MagicMock()
    batch = [{"page_id": "p1", "title": "T", "url": "u1", "body": "本文"}]
    ok = extract_terminology.process_batch(batch, set(), [], notion, "key", dry_run=True)
    assert ok is True
    save_mock.assert_not_called()
    notion.pages.update.assert_not_called()


def test_process_batch_continues_when_one_term_save_fails(monkeypatch):
    import extract_terminology
    fake_extract = MagicMock(return_value={
        "terms": [{"ja": "用語1"}, {"ja": "用語2"}], "style_rules": [],
    })
    monkeypatch.setattr(extract_terminology, "extract_terms_and_style", fake_extract)
    save_mock = MagicMock(side_effect=[Exception("notion error"), None])
    monkeypatch.setattr(extract_terminology, "save_pending_term", save_mock)
    notion = MagicMock()
    batch = [{"page_id": "p1", "title": "T", "url": "u1", "body": "本文"}]
    ok = extract_terminology.process_batch(batch, set(), [], notion, "key", dry_run=False)
    assert ok is True
    assert save_mock.call_count == 2
    notion.pages.update.assert_called_once()
```

- [ ] **Step 2: テストを実行して失敗を確認**

```bash
python -m pytest tests/test_extract_terminology.py -v -k process_batch
```
Expected: FAIL with `ImportError`

- [ ] **Step 3: process_batchとmainを実装**

```python
# ============================================================
# バッチ処理オーケストレーション
# ============================================================

def process_batch(batch: list[dict], known_ja: set[str], style_candidates: list[dict],
                   notion: Client, api_key: str, dry_run: bool) -> bool:
    """1バッチ（本文取得済みrelease群）を処理する。
    Claude API呼び出しが2回失敗した場合はFalseを返し、呼び出し側は「用語抽出済み」をONにしない。
    """
    combined_text = "\n\n---\n\n".join(r["body"] for r in batch)

    result = None
    for attempt in range(2):
        try:
            result = extract_terms_and_style(combined_text, api_key=api_key)
            break
        except Exception as e:
            print(f"  ⚠️  Claude API呼び出し失敗（{attempt + 1}/2回目）: {e}")
    if result is None:
        print("  ❌ このバッチをスキップします")
        return False

    # バッチ内の代表releaseを抽出元として記録する（10件分の本文を1回で渡すため、
    # 用語・ルールごとの厳密な出典追跡はスコープ外という設計判断に基づく簡略化）
    representative = batch[0]

    new_terms = dedupe_new_terms(result["terms"], known_ja)
    for term in new_terms:
        if dry_run:
            print(f"  [DRY RUN] 用語登録予定: {term.get('ja')}")
            continue
        try:
            save_pending_term(notion, term, representative["url"])
            print(f"  📝 用語登録: {term.get('ja')}")
        except Exception as e:
            print(f"  ⚠️  用語登録失敗（スキップ）: {term.get('ja')} ({e})")

    for rule in result["style_rules"]:
        style_candidates.append({
            "rule": rule.get("rule", ""),
            "example": rule.get("example", ""),
            "source_title": representative["title"],
            "source_url": representative["url"],
        })

    if not dry_run:
        for release in batch:
            notion.pages.update(
                page_id=release["page_id"],
                properties={EXTRACTED_PROPERTY: {"checkbox": True}},
            )
    return True


def main():
    parser = argparse.ArgumentParser(description="PR Timesコーパスから用語・表記ルールを抽出")
    parser.add_argument("--max", type=int, default=0, help="処理するリリース件数の上限（0=全件）")
    parser.add_argument("--batch-size", type=int, default=10, help="Claude APIに渡す1バッチあたりのリリース件数")
    parser.add_argument("--dry-run", action="store_true", help="Notionへの書き込みをせずプレビュー")
    args = parser.parse_args()

    notion_token = os.environ.get("NOTION_TOKEN")
    anthropic_key = os.environ.get("ANTHROPIC_KEY")
    if not notion_token or not anthropic_key:
        print("❌ 環境変数 NOTION_TOKEN / ANTHROPIC_KEY が設定されていません")
        sys.exit(1)

    notion = Client(auth=notion_token)

    print("🔧 Notionスキーマを確認中...")
    ensure_checkbox_property(notion)
    ensure_category_property(notion)

    print("📡 未処理のリリースを取得中...")
    releases = get_unprocessed_releases(notion, max_count=args.max)
    if not releases:
        print("✅ 未処理のリリースはありません")
        return
    print(f"✅ {len(releases)}件の未処理リリース")

    print("🔍 既存の用語集を読み込み中...")
    known_ja = get_existing_term_set(notion)
    print(f"   既存用語: {len(known_ja)}件")

    batches = build_batches(releases, args.batch_size)
    style_candidates: list[dict] = []
    processed_count = 0

    for i, batch in enumerate(batches, 1):
        print(f"\n[バッチ {i}/{len(batches)}] {len(batch)}件")
        ready = fetch_bodies_for_batch(batch)
        if not ready:
            print("  ⚠️  本文を取得できたreleaseがないためスキップ")
            continue
        if process_batch(ready, known_ja, style_candidates, notion, anthropic_key, args.dry_run):
            processed_count += len(ready)

    print(f"\n📚 表記ルール候補 {len(style_candidates)}件を統合中...")
    consolidated = consolidate_style_rules(style_candidates, anthropic_key)

    if consolidated and not args.dry_run:
        page_id, confirm_heading_id = get_or_create_style_rules_page(notion)
        existing_texts = get_existing_rule_texts(notion, page_id)
        added = append_style_rules_to_page(notion, page_id, confirm_heading_id, consolidated, existing_texts)
        print(f"✅ 表記ルールを{added}件追記（重複{len(consolidated) - added}件はスキップ）")
    elif consolidated:
        for rule in consolidated:
            print(f"  [DRY RUN] 表記ルール追記予定: {rule['rule']}")

    print("\n" + "=" * 50)
    print(f"完了: 処理済みリリース {processed_count}件")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: テストを実行して成功を確認**

```bash
python -m pytest tests/test_extract_terminology.py -v -k process_batch
```
Expected: 4 passed

- [ ] **Step 5: 全テストを通しで実行**

```bash
python -m pytest tests/test_extract_terminology.py -v
```
Expected: 全件 passed（Task 1〜8の合計、約36件）

- [ ] **Step 6: コミット**

```bash
git add extract_terminology.py tests/test_extract_terminology.py
git commit -m "feat: バッチ処理オーケストレーションとCLIエントリポイントを実装"
```

---

### Task 9: 実データでの動作確認（--dry-run）と本番実行

**Files:** なし（コード変更なし、手動確認のみ）

**Interfaces:** なし

- [ ] **Step 1: 環境変数を設定**

PowerShell:
```powershell
$env:NOTION_TOKEN = "<実際のIntegration Token>"
$env:ANTHROPIC_KEY = "<実際のAPI Key>"
```

- [ ] **Step 2: 少数件・dry-runで動作確認**

```bash
cd "C:\Users\r.tanaka\Documents\Claude Cowork\Projects\PR_Automation"
python extract_terminology.py --max 5 --batch-size 5 --dry-run
```

確認ポイント:
- Notionスキーマ確認のログが出ること（「用語抽出済み」「カテゴリ」プロパティが初回のみ追加される）
- 5件のreleaseが取得され、本文取得→Claude API呼び出しが行われること
- `[DRY RUN]`ログのみで実際のNotion書き込み（pages.create / pages.update / blocks.children.append）が発生しないこと

- [ ] **Step 3: dry-runを外して少数件で本番実行**

```bash
python extract_terminology.py --max 5 --batch-size 5
```

確認ポイント:
- 用語集DBに新規pendingエントリが登録されること（Notion UIで確認）
- 対象releaseの「用語抽出済み」チェックボックスがONになること
- 「KRAFTON Japan 表記ルール」ページが新規作成され、「要確認」「承認済み」見出しと統合済みルールが追記されること

- [ ] **Step 4: 同じ5件で再実行し、差分再実行ロジックを確認**

```bash
python extract_terminology.py --max 5 --batch-size 5
```
Expected: 「✅ 未処理のリリースはありません」と出力され、何も書き込まれないこと（Step 3で処理済みのreleaseが再取得されないこと）

- [ ] **Step 5: 残り全件を本番実行**

```bash
python extract_terminology.py
```

- [ ] **Step 6: README.mdに使い方を追記してコミット**

`README.md`を以下に更新:
```markdown
# PR_Automation_localization_system
自動ローカライズ

## スクリプト
- `prtimes_to_notion.py` — PR TimesのリリースをニュースリリースDBへ一括インポート
- `extract_terminology.py` — ニュースリリースDBの原稿から用語・表記ルールを抽出
- `localize_app.py` — Streamlit UI。KR/EN原稿の日本語訳とNotion登録
```

```bash
git add README.md
git commit -m "docs: extract_terminology.pyの使い方をREADMEに追記"
```

---

## Self-Review

**Spec coverage:**
- 本文取得の再利用（`fetch_article_body`をimport）→ Task 1, 3 ✅
- 差分再実行（「用語抽出済み」チェックボックス自動追加・フィルタ・処理後ON）→ Task 2, 3, 8 ✅
- 10件バッチ・Claude 1回呼び出しで用語+表記ルール同時抽出 → Task 5, 8 ✅
- 用語の重複排除・即時pending登録（KR表記は空欄のまま） → Task 1, 4, 8 ✅
- 表記ルールのメモリ蓄積→全バッチ後に統合→ページ追記（完全一致チェック） → Task 6, 7, 8 ✅
- ページが存在しない場合の新規作成（要確認/承認済み見出し） → Task 7 ✅
- CLIオプション（--dry-run, --max, --batch-size） → Task 8 ✅
- エラー処理（本文取得失敗/Claude APIリトライ/Notion書き込み失敗） → Task 3, 8 ✅
- テスト方針に列挙された5項目（重複排除/JSON解析頑健性/バッチ分割/統合パス入力構築/チェックボックス自動追加） → すべてTask 1, 2, 6でカバー ✅
- 汎用シグネチャ`extract_terms_and_style(text_ja, text_kr="", text_en="")` → Task 5 ✅

**Placeholder scan:** 全ステップに実コードを記載済み。TBD/TODO等は存在しない。

**Type consistency:** `release`辞書のキー（`page_id`/`title`/`url`/`body`）、`term`辞書のキー（`ja`/`en`/`category`）、`rule`辞書のキー（`rule`/`example`/`sources`/`source_title`/`source_url`）はTask 1〜8を通じて一貫している。

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-25-extract-terminology-implementation.md`. Two execution options:

1. **Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration
2. **Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
