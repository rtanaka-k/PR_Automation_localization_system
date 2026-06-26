"""
表記ルール抽出・統合のみを行うスタンドアロンスクリプト。
チェックボックスの状態に関わらず全リリースを対象に表記ルールを抽出し、
streaming APIで統合してNotionページへ追記する。
用語集DBへの書き込みは行わない。

使い方:
  $env:PYTHONUTF8="1"; $env:PYTHONIOENCODING="utf-8"
  (環境変数 NOTION_TOKEN, ANTHROPIC_KEY を設定)
  python -u extract_style_rules_only.py
"""

import os
import sys
import re
import json
import time
from pathlib import Path
from notion_client import Client
from anthropic import Anthropic

from prtimes_to_notion import fetch_article_body

NOTION_RELEASE_DATA_SOURCE_ID = "68f52f52-f7dc-4ab4-9941-61c7c7d7f6e7"
NOTION_STYLE_RULES_PAGE_ID = "38aed383-98b4-80a4-a51b-e165c271d81d"
CLAUDE_MODEL = "claude-sonnet-4-6"
BATCH_SIZE = 10
CONSOLIDATE_CHUNK = 150

CONFIRM_HEADING = "要確認"

# ============================================================
# プロセス管理
# ============================================================
_SCRIPT_DIR = Path(__file__).parent
_PID_FILE = _SCRIPT_DIR / "extract_style_rules_only.pid"
_PROGRESS_FILE = _SCRIPT_DIR / "extract_style_rules_progress.json"
_SAVE_INTERVAL = 5


def _check_already_running() -> None:
    if _PID_FILE.exists():
        try:
            old_pid = int(_PID_FILE.read_text().strip())
            os.kill(old_pid, 0)
            print(f"[ERROR] 既に実行中です (PID {old_pid})。停止後に再実行してください。")
            sys.exit(1)
        except OSError:
            pass
    _PID_FILE.write_text(str(os.getpid()))


def _cleanup_pid() -> None:
    _PID_FILE.unlink(missing_ok=True)


def _save_progress(candidates: list[dict], completed_batches: int) -> None:
    tmp = str(_PROGRESS_FILE) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"completed": completed_batches, "candidates": candidates}, f, ensure_ascii=False)
    Path(tmp).replace(_PROGRESS_FILE)
    print(f"  💾 進捗保存（{completed_batches}バッチ完了、候補{len(candidates)}件）", flush=True)


def _load_progress() -> tuple[list[dict], int]:
    if _PROGRESS_FILE.exists():
        with open(_PROGRESS_FILE, encoding="utf-8") as f:
            d = json.load(f)
        print(f"⏩ 進捗復元: {d['completed']}バッチ完了、候補{len(d['candidates'])}件")
        return d["candidates"], d["completed"]
    return [], 0


def get_all_releases(notion: Client) -> list[dict]:
    releases = []
    cursor = None
    while True:
        params = {"data_source_id": NOTION_RELEASE_DATA_SOURCE_ID, "page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        res = notion.data_sources.query(**params)
        for page in res["results"]:
            props = page["properties"]
            title_items = props.get("タイトル（日本語）", {}).get("title", [])
            title = "".join(t.get("plain_text", "") for t in title_items)
            url = props.get("PR Times URL", {}).get("url") or ""
            releases.append({"page_id": page["id"], "title": title, "url": url})
        if not res.get("has_more"):
            break
        cursor = res["next_cursor"]
    return releases


def extract_style_rules_from_batch(releases: list[dict], client: Anthropic) -> list[dict]:
    bodies = []
    for r in releases:
        body = fetch_article_body(r["url"]) if r["url"] else ""
        if body:
            bodies.append({"title": r["title"], "url": r["url"], "body": body})
        time.sleep(0.5)

    if not bodies:
        return []

    combined = "\n\n---\n\n".join(
        f"【{b['title']}】\n{b['body'][:3000]}" for b in bodies
    )
    del bodies

    prompt = f"""以下のプレスリリース本文から、文体・表記に関するルール候補を抽出してください。

対象:
- 記号の使い方（「」『』の使い分け、（）の使い方など）
- 数字・単位の書き方（半角/全角、スペースの有無など）
- 固有名詞の表記パターン（大文字/小文字、スペースの有無など）
- その他、一貫した表記習慣

【出力形式】JSON配列のみ（説明不要）
[
  {{"rule": "ルールの説明", "example": "具体例（任意）", "source_title": "出典記事タイトル", "source_url": "出典URL"}}
]

【プレスリリース本文】
{combined}
"""
    with client.messages.stream(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        raw = stream.get_final_text()

    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def consolidate_chunk(candidates: list[dict], client: Anthropic) -> list[dict]:
    lines = []
    for i, c in enumerate(candidates, 1):
        suffix = ""
        if c.get("example"):
            suffix += f"（例: {c['example']}）"
        if c.get("source_title"):
            suffix += f"（出典: {c['source_title']}）"
        lines.append(f"{i}. {c['rule']}{suffix}")

    prompt = f"""以下の表記ルール候補を統合し、重複を除去した最終リストにしてください。

【候補】
{chr(10).join(lines)}

【出力形式】JSON配列のみ
[
  {{"rule": "統合後のルール", "example": "代表的な具体例（任意）", "source_title": "出典（任意）", "source_url": ""}}
]
"""
    with client.messages.stream(
        model=CLAUDE_MODEL,
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        raw = stream.get_final_text()

    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def get_existing_rule_texts(notion: Client, page_id: str) -> set[str]:
    existing = set()
    cursor = None
    while True:
        params = {"block_id": page_id, "page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        res = notion.blocks.children.list(**params)
        for block in res["results"]:
            bt = block.get("type", "")
            texts = block.get(bt, {}).get("rich_text", [])
            text = "".join(t.get("plain_text", "") for t in texts)
            if text:
                existing.add(text.split("\n")[0])
        if not res.get("has_more"):
            break
        cursor = res["next_cursor"]
    return existing


def get_or_create_confirm_heading(notion: Client, page_id: str) -> str:
    cursor = None
    while True:
        params = {"block_id": page_id, "page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        res = notion.blocks.children.list(**params)
        for block in res["results"]:
            bt = block.get("type", "")
            texts = block.get(bt, {}).get("rich_text", [])
            text = "".join(t.get("plain_text", "") for t in texts)
            if text == CONFIRM_HEADING:
                return block["id"]
        if not res.get("has_more"):
            break
        cursor = res["next_cursor"]

    res = notion.blocks.children.append(
        block_id=page_id,
        children=[{
            "object": "block",
            "type": "heading_2",
            "heading_2": {
                "rich_text": [{"type": "text", "text": {"content": CONFIRM_HEADING}}]
            },
        }],
    )
    return res["results"][0]["id"]


def append_rules_to_page(notion: Client, page_id: str, confirm_heading_id: str,
                          rules: list[dict], existing: set[str]) -> int:
    new_blocks = []
    for rule in rules:
        rule_text = rule.get("rule", "").strip()
        if not rule_text or rule_text in existing:
            continue
        lines = [rule_text]
        if rule.get("example"):
            lines.append(f"例: {rule['example']}")
        if rule.get("source_title"):
            src = f"出典: {rule['source_title']}"
            if rule.get("source_url"):
                src += f"（{rule['source_url']}）"
            lines.append(src)
        content = "\n".join(lines)
        new_blocks.append({
            "object": "block",
            "type": "bulleted_list_item",
            "bulleted_list_item": {
                "rich_text": [{"type": "text", "text": {"content": content[:2000]}}]
            },
        })
        existing.add(rule_text)

    if not new_blocks:
        return 0

    # Notion APIは1リクエスト100ブロックまで。page_idにafter=heading_idで位置指定
    for i in range(0, len(new_blocks), 100):
        chunk = new_blocks[i:i + 100]
        if i == 0:
            notion.blocks.children.append(
                block_id=page_id, children=chunk, after=confirm_heading_id
            )
        else:
            notion.blocks.children.append(block_id=page_id, children=chunk)

    return len(new_blocks)


def main():
    notion_token = os.environ.get("NOTION_TOKEN")
    anthropic_key = os.environ.get("ANTHROPIC_KEY")
    if not notion_token or not anthropic_key:
        print("エラー: NOTION_TOKEN または ANTHROPIC_KEY が設定されていません")
        sys.exit(1)

    _check_already_running()

    notion = Client(auth=notion_token)
    client = Anthropic(api_key=anthropic_key)

    all_candidates, completed_batches = _load_progress()
    current_batch_idx = completed_batches

    try:
        print("全リリース取得中...")
        releases = get_all_releases(notion)
        print(f"  {len(releases)} 件取得")

        batches = [releases[i:i + BATCH_SIZE] for i in range(0, len(releases), BATCH_SIZE)]

        for i, batch in enumerate(batches, 1):
            current_batch_idx = i
            if i <= completed_batches:
                print(f"[バッチ {i}/{len(batches)}] スキップ（保存済み）")
                continue
            print(f"[バッチ {i}/{len(batches)}] 表記ルール抽出中...", flush=True)
            candidates = extract_style_rules_from_batch(batch, client)
            all_candidates.extend(candidates)
            print(f"  累計候補: {len(all_candidates)} 件")
            if i % _SAVE_INTERVAL == 0:
                _save_progress(all_candidates, i)

        # バッチ完了 → 進捗ファイル不要
        _PROGRESS_FILE.unlink(missing_ok=True)

        print(f"\n表記ルール候補 {len(all_candidates)} 件を統合中（チャンク分割）...")
        chunks = [all_candidates[i:i + CONSOLIDATE_CHUNK]
                  for i in range(0, len(all_candidates), CONSOLIDATE_CHUNK)]

        partial_results: list[dict] = []
        for i, chunk in enumerate(chunks, 1):
            print(f"  チャンク {i}/{len(chunks)} を統合中 ({len(chunk)} 件)...", flush=True)
            result = consolidate_chunk(chunk, client)
            partial_results.extend(result)
            print(f"  チャンク統合後: {len(partial_results)} 件")

        final_results = partial_results
        print(f"最終ルール数: {len(final_results)} 件")

        if not final_results:
            print("統合結果が空のため終了")
            return

        print("\nNotionページに追記中...")
        existing = get_existing_rule_texts(notion, NOTION_STYLE_RULES_PAGE_ID)
        confirm_heading_id = get_or_create_confirm_heading(notion, NOTION_STYLE_RULES_PAGE_ID)
        added = append_rules_to_page(notion, NOTION_STYLE_RULES_PAGE_ID,
                                      confirm_heading_id, final_results, existing)
        print(f"✅ 表記ルールを {added} 件追記（重複 {len(final_results) - added} 件はスキップ）")
        print("\n完了")

    except KeyboardInterrupt:
        _save_progress(all_candidates, current_batch_idx)
        print(f"\n⚠️  中断。進捗を保存しました（{current_batch_idx}バッチまで）")
        sys.exit(1)
    finally:
        client.close()
        _cleanup_pid()


if __name__ == "__main__":
    main()
