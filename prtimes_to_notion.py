"""
KRAFTON Japan PR Times → Notion DB インポートスクリプト
======================================================
PR Times (company_id=82433) のリリースを取得し、
Notion DB「KRAFTON Japan ニュースリリース DB」に保存します。

使い方:
  1. 依存パッケージをインストール
     pip install feedparser requests beautifulsoup4 notion-client

  2. Notion Integration Token を環境変数に設定
     Windows PowerShell:
       $env:NOTION_TOKEN = "ntn_xxxxxxxxxxxx"
     macOS/Linux:
       export NOTION_TOKEN="ntn_xxxxxxxxxxxx"

  3. 実行
     python prtimes_to_notion.py

     オプション:
       --full-content   各記事ページを取得して本文をNotionページに保存（デフォルト: ON）
       --max 50         取得件数の上限（デフォルト: 全件）
       --dry-run        Notionへの書き込みをせず内容をプレビュー

Notion Integration Token の取得方法:
  https://www.notion.so/my-integrations でインテグレーションを作成し、
  作成したDBページを「接続先に追加」してください。
"""

import os
import sys
import time
import argparse
import feedparser
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from notion_client import Client

# ============================================================
# 設定
# ============================================================
NOTION_DATABASE_ID = "7606be38ff494d1f84902f8f82ebfee0"
PRTIMES_RSS_URL = "https://prtimes.jp/rss/company/82433/"
PRTIMES_COMPANY_URL = "https://prtimes.jp/main/html/searchrlp/company_id/82433"

# タイトルキーワードからタイトルタグを自動付与
TITLE_TAG_RULES = {
    "PUBG": ["PUBG", "PlayerUnknown"],
    "inZOI": ["inZOI", "インゾイ"],
    "Subnautica": ["Subnautica", "サブノーティカ"],
    "KRAFTON": ["KRAFTON", "クラフトン"],
}

# タイトルキーワードからカテゴリを自動付与
CATEGORY_RULES = {
    "コラボ": ["コラボ", "コラボレーション", "タイアップ", "連携", "パートナー"],
    "アップデート": ["アップデート", "update", "パッチ", "新シーズン", "追加"],
    "大会・イベント": ["大会", "トーナメント", "イベント", "eスポーツ", "esports", "選手権", "カップ"],
    "IR・経営": ["決算", "業績", "IR", "経営", "取締役", "代表", "組織変更", "資本"],
    "採用": ["採用", "求人", "募集", "入社"],
}

# ============================================================
# ヘルパー関数
# ============================================================

def detect_title_tags(title: str) -> list[str]:
    """タイトルからタイトルタグを自動検出"""
    tags = []
    title_lower = title.lower()
    for tag, keywords in TITLE_TAG_RULES.items():
        if any(kw.lower() in title_lower for kw in keywords):
            tags.append(tag)
    return tags or ["その他"]


def detect_categories(title: str) -> list[str]:
    """タイトルからカテゴリを自動検出"""
    categories = []
    title_lower = title.lower()
    for category, keywords in CATEGORY_RULES.items():
        if any(kw.lower() in title_lower for kw in keywords):
            categories.append(category)
    return categories or ["その他"]


def fetch_article_body(url: str) -> str:
    """PR Timesの記事ページから本文を取得"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    try:
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # PR Timesの本文セレクタ
        body_el = (
            soup.select_one("div.articleBody")
            or soup.select_one("div.press-release-detail__body")
            or soup.select_one("section.press-release-detail")
        )
        if body_el:
            return body_el.get_text(separator="\n", strip=True)
        return ""
    except Exception as e:
        print(f"  ⚠️  本文取得失敗: {e}")
        return ""


def parse_published_date(entry) -> str | None:
    """RSSエントリから配信日をISO 8601形式で取得"""
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        return dt.date().isoformat()
    return None


def get_existing_urls(notion: Client) -> set[str]:
    """NotionDBに既に登録されているPR Times URLの一覧を取得（重複防止）"""
    urls = set()
    cursor = None
    while True:
        params = {
            "database_id": NOTION_DATABASE_ID,
            "page_size": 100,
            "filter": {"property": "PR Times URL", "url": {"is_not_empty": True}},
        }
        if cursor:
            params["start_cursor"] = cursor
        res = notion.databases.query(**params)
        for page in res["results"]:
            url = page["properties"].get("PR Times URL", {}).get("url")
            if url:
                urls.add(url)
        if not res.get("has_more"):
            break
        cursor = res["next_cursor"]
    return urls


def create_notion_page(notion: Client, entry: dict, body: str, dry_run: bool = False):
    """Notionにページを作成"""
    title_ja = entry["title"]
    url = entry["url"]
    published_date = entry["published_date"]
    title_tags = detect_title_tags(title_ja)
    categories = detect_categories(title_ja)

    properties = {
        "タイトル（日本語）": {"title": [{"text": {"content": title_ja}}]},
        "タイトル（原文）": {"rich_text": [{"text": {"content": title_ja}}]},
        "PR Times URL": {"url": url},
        "ステータス": {"select": {"name": "raw"}},
        "タイトルタグ": {"multi_select": [{"name": t} for t in title_tags]},
        "カテゴリ": {"multi_select": [{"name": c} for c in categories]},
        "備考": {"rich_text": [{"text": {"content": "PR Timesより自動インポート"}}]},
    }
    if published_date:
        properties["配信日"] = {"date": {"start": published_date}}

    if dry_run:
        print(f"  [DRY RUN] 作成予定: {title_ja}")
        print(f"    URL: {url}")
        print(f"    配信日: {published_date}")
        print(f"    タグ: {title_tags}  カテゴリ: {categories}")
        return

    # Notionページ作成
    page = notion.pages.create(
        parent={"database_id": NOTION_DATABASE_ID},
        properties=properties,
    )
    page_id = page["id"]

    # 本文をページのブロックとして追加
    if body:
        # 2000文字ずつ分割（Notionのブロック文字数制限対応）
        chunks = [body[i:i+1999] for i in range(0, len(body), 1999)]
        children = []

        # 見出し
        children.append({
            "object": "block",
            "type": "heading_2",
            "heading_2": {"rich_text": [{"text": {"content": "本文（原文）"}}]},
        })
        # 本文ブロック
        for chunk in chunks[:50]:  # Notionの一度のappend上限対応
            children.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [{"text": {"content": chunk}}]},
            })

        notion.blocks.children.append(block_id=page_id, children=children)

    return page_id


# ============================================================
# メイン処理
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="PR Times → Notion インポート")
    parser.add_argument("--max", type=int, default=0, help="取得件数の上限（0=全件）")
    parser.add_argument("--no-full-content", action="store_true", help="本文取得をスキップ")
    parser.add_argument("--dry-run", action="store_true", help="Notionへの書き込みをせずプレビュー")
    args = parser.parse_args()

    token = os.environ.get("NOTION_TOKEN")
    if not token:
        print("❌ 環境変数 NOTION_TOKEN が設定されていません")
        print("   例: $env:NOTION_TOKEN = 'ntn_xxxxxxxxxxxx'")
        sys.exit(1)

    notion = Client(auth=token)

    print("📡 PR Times RSSを取得中...")
    feed = feedparser.parse(PRTIMES_RSS_URL)
    entries = feed.entries

    if not entries:
        print("⚠️  RSSエントリが0件です。company_idを確認してください")
        print(f"   URL: {PRTIMES_RSS_URL}")
        sys.exit(1)

    print(f"✅ {len(entries)}件のリリースを取得")

    if args.max > 0:
        entries = entries[: args.max]
        print(f"   （上限指定により {args.max} 件に絞り込み）")

    # 既存URL取得（重複チェック用）
    if not args.dry_run:
        print("🔍 Notion既存レコードを確認中...")
        existing_urls = get_existing_urls(notion)
        print(f"   既存: {len(existing_urls)}件")
    else:
        existing_urls = set()

    # インポート実行
    new_count = 0
    skip_count = 0

    for i, entry in enumerate(entries, 1):
        title = entry.get("title", "（タイトルなし）")
        url = entry.get("link", "")
        published_date = parse_published_date(entry)

        print(f"\n[{i}/{len(entries)}] {title}")

        if url in existing_urls:
            print("  ⏭️  既存レコードのためスキップ")
            skip_count += 1
            continue

        # 本文取得
        body = ""
        if not args.no_full_content and url:
            print("  📄 本文取得中...")
            body = fetch_article_body(url)
            time.sleep(1)  # サーバー負荷軽減

        entry_data = {
            "title": title,
            "url": url,
            "published_date": published_date,
        }

        create_notion_page(notion, entry_data, body, dry_run=args.dry_run)

        if not args.dry_run:
            existing_urls.add(url)
            new_count += 1
            print(f"  ✅ 登録完了")
            time.sleep(0.5)  # Notion API レート制限対応

    # 完了レポート
    print("\n" + "=" * 50)
    print(f"完了: 新規登録 {new_count}件 / スキップ {skip_count}件")
    if not args.dry_run:
        print(f"Notion DB: https://app.notion.com/p/{NOTION_DATABASE_ID.replace('-', '')}")


if __name__ == "__main__":
    main()
