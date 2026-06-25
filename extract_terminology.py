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
