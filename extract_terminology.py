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
        max_tokens=8192,
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
        max_tokens=8192,
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
    """「KRAFTON Japan 表記ルール」ページを取得する。なければ新規作成する。"""
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

    new_terms = dedupe_new_terms(result.get("terms", []), known_ja)
    for term in new_terms:
        if dry_run:
            print(f"  [DRY RUN] 用語登録予定: {term.get('ja')}")
            continue
        try:
            save_pending_term(notion, term, representative["url"])
            print(f"  📝 用語登録: {term.get('ja')}")
        except Exception as e:
            print(f"  ⚠️  用語登録失敗（スキップ）: {term.get('ja')} ({e})")

    for rule in result.get("style_rules", []):
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
    if args.dry_run:
        print("  ℹ️  [DRY RUN] スキーマプロパティの追加（存在しない場合のみ）は実行されます。用語登録・チェックボックス更新・表記ルール追記は行いません。")

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
        try:
            ready = fetch_bodies_for_batch(batch)
            if not ready:
                print("  ⚠️  本文を取得できたreleaseがないためスキップ")
                continue
            if process_batch(ready, known_ja, style_candidates, notion, anthropic_key, args.dry_run):
                processed_count += len(ready)
        except Exception as e:
            print(f"  ❌ バッチ{i}で予期しないエラーが発生したためスキップ: {e}")

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
