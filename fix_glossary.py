"""
用語集DB 表記修正スクリプト
============================
ニュースリリース本文の表記を正として、以下を実施する:
1. カタカナ読みで登録されているEN用語 → 本文通りの英語表記に更新
2. 重複エントリ（表記ゆれ）のうち誤った方を削除
"""

import os
import sys
from notion_client import Client

NOTION_GLOSSARY_DB_ID = "8959e88bcfe34936a59cc82232215266"
NOTION_GLOSSARY_DATA_SOURCE_ID = "0d2447b5-6fcf-414b-a3b4-88dad64f6b8b"

# JP表記の更新マップ: {現在の誤った表記: 正しい表記}
UPDATES = {
    "マックオーエス":               "macOS",
    "メタル":                       "Metal",
    "メタルエフエックス":            "MetalFX",
    "コア エムエル":                 "Core ML",
    "エムワン":                      "Apple M1",
    "エムスリー":                    "M3チップ",
    "エムフォー":                    "M4チップ",
    "エピック ゲームズ ストア":      "Epic Games Store",
    "アップル アップ ストア":        "App Store",
    "マック アップ ストア":          "Mac App Store",
    "スチーム ネクスト フェスト":    "Steam Next Fest",
    "パブジー スタジオズ":           "PUBG STUDIOS",
    "ストライキング ディスタンス スタジオズ": "Striking Distance Studios",
    "リル ゲームズ":                 "ReLU Games",
    "アップルシリコン":              "Appleシリコン",
    "グーグル プレイ":               "Google Play",
    # A項目: 5minlabはプレスリリース本文通り英語表記に統一
    "ファイブミニラボ":              "5minlab",
    "5ミンラボ":                     "5minlab",
}

# 削除リスト: 重複ペアのうち誤った方（ニュースリリース本文にない表記）
DELETES = {
    "Microsoftストア",          # 正: Microsoft Store（英語）
    "Epic Gamesストア",         # 正: Epic Games Store（英語）
    "パブジー ニュー ステート",  # 正: PUBG：ニュー・ステート（公式JP表記）
    "オールド・フェリー・ドーナツ",  # 正: Old Ferry Donut（英語）
    "オラク",                   # 正: Orak（英語）
    # B-2: 本文に存在しない疑いのある用語
    "マヤ",                     # Maya → リリース本文に記載なし
    "ブレンダー",               # Blender → リリース本文に記載なし
    "カース フォージ",           # CurseForge → リリース本文に記載なし
    "バーチャルライバー",        # Virtual Liver → ブルーロックリリースに該当語なし
}


def get_all_glossary_entries(notion: Client) -> list[dict]:
    entries = []
    cursor = None
    while True:
        params = {"data_source_id": NOTION_GLOSSARY_DATA_SOURCE_ID, "page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        res = notion.data_sources.query(**params)
        for page in res["results"]:
            props = page["properties"]
            title_items = props.get("日本語表記", {}).get("title", [])
            ja = "".join(t.get("plain_text", "") for t in title_items)
            entries.append({"page_id": page["id"], "ja": ja})
        if not res.get("has_more"):
            break
        cursor = res["next_cursor"]
    return entries


def main():
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        print("エラー: NOTION_TOKEN が設定されていません")
        sys.exit(1)

    notion = Client(auth=token)

    print("用語集エントリを取得中...")
    entries = get_all_glossary_entries(notion)
    print(f"  {len(entries)} 件取得")

    # 現在のJP表記 → ページIDのマップを作成
    ja_to_page_id: dict[str, list[str]] = {}
    for e in entries:
        ja = e["ja"]
        if ja not in ja_to_page_id:
            ja_to_page_id[ja] = []
        ja_to_page_id[ja].append(e["page_id"])

    updated = 0
    deleted = 0
    skipped = 0

    print("\n--- 更新処理 ---")
    for old_ja, new_ja in UPDATES.items():
        page_ids = ja_to_page_id.get(old_ja, [])
        if not page_ids:
            print(f"  スキップ（存在しない）: {old_ja}")
            skipped += 1
            continue

        # 更新先のJP表記が既に存在する場合は重複になるので削除
        if new_ja in ja_to_page_id:
            for page_id in page_ids:
                notion.pages.update(page_id=page_id, archived=True)
                deleted += 1
            print(f"  削除（更新先 '{new_ja}' が既存）: {old_ja} × {len(page_ids)}件")
        else:
            for page_id in page_ids:
                notion.pages.update(
                    page_id=page_id,
                    properties={"日本語表記": {"title": [{"text": {"content": new_ja}}]}},
                )
                updated += 1
            # マップを更新
            ja_to_page_id[new_ja] = page_ids
            del ja_to_page_id[old_ja]
            print(f"  更新: {old_ja} → {new_ja}")

    print("\n--- 削除処理 ---")
    for ja in DELETES:
        page_ids = ja_to_page_id.get(ja, [])
        if not page_ids:
            print(f"  スキップ（存在しない）: {ja}")
            skipped += 1
            continue
        for page_id in page_ids:
            notion.pages.update(page_id=page_id, archived=True)
            deleted += 1
        print(f"  削除: {ja} × {len(page_ids)}件")

    print(f"\n完了: 更新 {updated}件 / 削除 {deleted}件 / スキップ {skipped}件")


if __name__ == "__main__":
    main()
