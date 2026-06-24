"""
KRAFTON Japan PR Times → Notion DB インポートスクリプト
======================================================
PR Times (company_id=82433) のリリースを取得し、
Notion DB「KRAFTON Japan ニュースリリース DB」に保存します。

使い方:
  1. 依存パッケージをインストール
     pip install requests beautifulsoup4 notion-client

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
import requests
from bs4 import BeautifulSoup
from notion_client import Client

# ============================================================
# 設定
# ============================================================
NOTION_DATABASE_ID = "7606be38ff494d1f84902f8f82ebfee0"
# notion-client 3.x（Notion API 2025-09-03）はクエリがdata_source単位になったため、
# databases.query()は廃止され、data_sources.query()にdata_source_idを渡す必要がある。
NOTION_DATA_SOURCE_ID = "68f52f52-f7dc-4ab4-9941-61c7c7d7f6e7"

PRTIMES_COMPANY_ID = 82433
# 公開RSS(/rss/company/{id}/)は廃止済み(404)。企業ページがJSレンダリングのSPAになった際に
# 内部的に呼んでいる非公開だが認証不要のJSON APIから一覧を取得する(Playwrightで通信を観察して特定)。
PRTIMES_PRESS_RELEASES_API = f"https://prtimes.jp/api/company_content.php/companies/{PRTIMES_COMPANY_ID}/press_releases"
PRTIMES_PAGE_SIZE = 100

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

        # PR Timesの本文セレクタ（press-release-body-v3-0-0は記事詳細ページの本文専用クラス。
        # CSS Modulesのハッシュ付きクラス名と違い再デプロイで変わりにくいため優先する）
        body_el = (
            soup.select_one("div.press-release-body-v3-0-0")
            or soup.select_one("article")
        )
        if body_el:
            return body_el.get_text(separator="\n", strip=True)
        return ""
    except Exception as e:
        print(f"  ⚠️  本文取得失敗: {e}")
        return ""


def fetch_press_releases(max_count: int = 0) -> list[dict]:
    """company_content.php APIから全リリースのメタデータ（タイトル・URL・配信日）を取得する。

    新しい順に返る。max_countを指定した場合はその件数で打ち切る（0=全件）。
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    entries = []
    skip = 0
    total = None

    while total is None or skip < total:
        if max_count and len(entries) >= max_count:
            break
        r = requests.get(
            PRTIMES_PRESS_RELEASES_API,
            headers=headers,
            params={"skip": skip, "limit": PRTIMES_PAGE_SIZE},
            timeout=15,
        )
        r.raise_for_status()
        payload = r.json()["data"]
        total = payload["total"]

        for item in payload["data"]:
            entries.append({
                "title": item.get("title", "（タイトルなし）"),
                "link": "https://prtimes.jp" + item["url"],
                "published_date": (item.get("release_comple_date") or "")[:10] or None,
            })

        skip += PRTIMES_PAGE_SIZE

    return entries[:max_count] if max_count else entries


def get_existing_urls(notion: Client) -> set[str]:
    """NotionDBに既に登録されているPR Times URLの一覧を取得（重複防止）"""
    urls = set()
    cursor = None
    while True:
        params = {
            "data_source_id": NOTION_DATA_SOURCE_ID,
            "page_size": 100,
            "filter": {"property": "PR Times URL", "url": {"is_not_empty": True}},
        }
        if cursor:
            params["start_cursor"] = cursor
        res = notion.data_sources.query(**params)
        for page in res["results"]:
            url = page["properties"].get("PR Times URL", {}).get("url")
            if url:
                urls.add(url)
        if not res.get("has_more"):
            break
        cursor = res["next_cursor"]
    return urls


def chunk_text_for_notion(text: str, limit: int = 2000) -> list[str]:
    """Notionのrich_text文字数制限（UTF-16コードユニット数）に基づいてテキストを分割する。

    Pythonのlen()はUnicodeコードポイント数を数えるが、絵文字等のサロゲートペア文字は
    UTF-16では2ユニット消費するため、コードポイント数で機械的に切ると2000を超える場合がある。
    """
    chunks = []
    current: list[str] = []
    current_len = 0
    for ch in text:
        ch_len = 2 if ord(ch) > 0xFFFF else 1
        if current_len + ch_len > limit and current:
            chunks.append("".join(current))
            current = []
            current_len = 0
        current.append(ch)
        current_len += ch_len
    if current:
        chunks.append("".join(current))
    return chunks


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
        chunks = chunk_text_for_notion(body)
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

    print("📡 PR Timesからリリース一覧を取得中...")
    entries = fetch_press_releases(max_count=args.max)

    if not entries:
        print("⚠️  リリースが0件です。company_idを確認してください")
        print(f"   API: {PRTIMES_PRESS_RELEASES_API}")
        sys.exit(1)

    print(f"✅ {len(entries)}件のリリースを取得")
    if args.max > 0:
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
        published_date = entry.get("published_date")

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
