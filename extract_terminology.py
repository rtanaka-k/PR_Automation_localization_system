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
    """表記ルール1件をNotionの�条書きブロック用テキストに整形する。
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
