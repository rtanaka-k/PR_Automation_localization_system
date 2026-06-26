"""
「用語抽出済み」チェックボックスを全件Falseにリセットするスクリプト。
表記ルール統合の再実行準備用。

使い方:
  $env:PYTHONUTF8="1"; $env:PYTHONIOENCODING="utf-8"
  $env:NOTION_TOKEN = "ntn_..."
  python reset_extracted_flags.py
"""

import os
import sys
from notion_client import Client

NOTION_RELEASE_DATA_SOURCE_ID = "68f52f52-f7dc-4ab4-9941-61c7c7d7f6e7"
EXTRACTED_PROPERTY = "用語抽出済み"


def main():
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        print("エラー: NOTION_TOKEN が設定されていません")
        sys.exit(1)

    notion = Client(auth=token)

    pages = []
    cursor = None
    print("チェックボックスがTrueのページを取得中...")
    while True:
        params = {
            "data_source_id": NOTION_RELEASE_DATA_SOURCE_ID,
            "page_size": 100,
            "filter": {"property": EXTRACTED_PROPERTY, "checkbox": {"equals": True}},
        }
        if cursor:
            params["start_cursor"] = cursor
        res = notion.data_sources.query(**params)
        pages.extend(res["results"])
        print(f"  取得済み: {len(pages)}件")
        if not res.get("has_more"):
            break
        cursor = res["next_cursor"]

    print(f"\n合計 {len(pages)} 件をリセットします...")
    for i, page in enumerate(pages, 1):
        notion.pages.update(
            page_id=page["id"],
            properties={EXTRACTED_PROPERTY: {"checkbox": False}},
        )
        if i % 50 == 0:
            print(f"  {i}/{len(pages)} 件完了")

    print(f"\n完了: {len(pages)} 件をFalseにリセットしました")


if __name__ == "__main__":
    main()
