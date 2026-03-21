#!/usr/bin/env python3
"""
sync_work_log.py - Gitコミット履歴を作業ログスプレッドシートに自動同期

使い方:
  python scripts/sync_work_log.py              # 未記録分を全件送信
  python scripts/sync_work_log.py --since 2026-03-18  # 指定日以降を送信
  python scripts/sync_work_log.py --dry-run    # 送信せず内容だけ確認

仕組み:
  1. git log からコミット履歴を取得
  2. .last_synced_hash ファイルで前回同期済みのコミットを記録
  3. 未同期分を webhook.gs の claude_session_log アクションで GAS に POST
"""

import subprocess
import json
import sys
import os
import argparse
from datetime import datetime

# GAS_URL は環境変数 or デフォルト値
GAS_URL = os.environ.get(
    "GAS_URL",
    "https://script.google.com/macros/s/AKfycbx9JpYWvi3p0HgA9Bb0RLgEjkgzbF6iJRuAX7Ks2VL3hwIEnpuTR0J1ydtxegGKRXjh/exec"
)

LAST_SYNCED_FILE = os.path.join(os.path.dirname(__file__), ".last_synced_hash")


def get_commits(since_hash=None, since_date=None):
    """git log からコミット一覧を取得（古い順）"""
    cmd = [
        "git", "log",
        "--format=%H||%ai||%an||%s||%b",
        "--reverse",
        "--name-only",
    ]
    if since_hash:
        cmd.append(f"{since_hash}..HEAD")
    elif since_date:
        cmd.append(f"--since={since_date}")

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=os.path.dirname(__file__) or ".")
    if result.returncode != 0:
        print(f"[ERROR] git log 失敗: {result.stderr}")
        return []

    commits = []
    current = None
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        if "||" in line and line.count("||") >= 3:
            if current:
                commits.append(current)
            parts = line.split("||", 4)
            current = {
                "hash": parts[0][:7],
                "full_hash": parts[0],
                "date": parts[1].strip(),
                "author": parts[2].strip(),
                "subject": parts[3].strip(),
                "body": parts[4].strip() if len(parts) > 4 else "",
                "files": [],
            }
        elif current:
            current["files"].append(line.strip())

    if current:
        commits.append(current)

    return commits


def get_last_synced_hash():
    """前回同期済みのコミットハッシュを取得"""
    if os.path.exists(LAST_SYNCED_FILE):
        with open(LAST_SYNCED_FILE, "r") as f:
            return f.read().strip()
    return None


def save_last_synced_hash(full_hash):
    """同期済みハッシュを保存"""
    with open(LAST_SYNCED_FILE, "w") as f:
        f.write(full_hash)


def send_to_gas(payload):
    """GASにPOSTリクエストを送信"""
    try:
        import httpx
        response = httpx.post(GAS_URL, json=payload, timeout=30, follow_redirects=True)
        result = response.json()
        return result.get("ok", False)
    except ImportError:
        # httpx がなければ urllib で代替
        import urllib.request
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            GAS_URL,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result.get("ok", False)


def format_timestamp(git_date_str):
    """git の日付文字列を YYYY/MM/DD HH:MM に変換"""
    try:
        dt = datetime.strptime(git_date_str[:19], "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%Y/%m/%d %H:%M")
    except (ValueError, IndexError):
        return git_date_str


def main():
    parser = argparse.ArgumentParser(description="Gitコミットを作業ログスプレッドシートに同期")
    parser.add_argument("--since", help="この日付以降のコミットを対象 (例: 2026-03-18)")
    parser.add_argument("--dry-run", action="store_true", help="送信せず内容だけ表示")
    parser.add_argument("--all", action="store_true", help="全コミットを対象（前回同期位置を無視）")
    args = parser.parse_args()

    # 対象コミットを取得
    if args.since:
        commits = get_commits(since_date=args.since)
        print(f"[sync_work_log] {args.since} 以降のコミットを取得: {len(commits)} 件")
    elif args.all:
        commits = get_commits()
        print(f"[sync_work_log] 全コミットを取得: {len(commits)} 件")
    else:
        last_hash = get_last_synced_hash()
        if last_hash:
            commits = get_commits(since_hash=last_hash)
            print(f"[sync_work_log] {last_hash[:7]}.. 以降の未同期コミット: {len(commits)} 件")
        else:
            print("[sync_work_log] 初回実行: --since で日付を指定するか --all で全件を対象にしてください")
            print("  例: python scripts/sync_work_log.py --since 2026-03-18")
            return

    if not commits:
        print("[sync_work_log] 未同期のコミットはありません")
        return

    # 送信
    success = 0
    for i, c in enumerate(commits, 1):
        files_str = ", ".join(f for f in c["files"] if f) or "(なし)"
        description = c["subject"]
        if c["body"]:
            description += f"\n{c['body']}"

        # Co-Authored-By 行から作業者を判定
        author = "Claude Code" if "Claude" in c["author"] or "Co-Authored-By: Claude" in description else c["author"]

        payload = {
            "action": "claude_session_log",
            "timestamp": format_timestamp(c["date"]),
            "author": author,
            "files": files_str,
            "description": description,
            "commit_hash": c["hash"],
            "note": "",
        }

        if args.dry_run:
            print(f"\n--- [{i}/{len(commits)}] {c['hash']} ---")
            print(f"  日時:     {payload['timestamp']}")
            print(f"  作業者:   {payload['author']}")
            print(f"  内容:     {c['subject']}")
            print(f"  ファイル: {files_str}")
        else:
            try:
                ok = send_to_gas(payload)
                status = "OK" if ok else "FAIL"
                print(f"  [{i}/{len(commits)}] {c['hash']} {c['subject'][:50]} ... {status}")
                if ok:
                    success += 1
                    save_last_synced_hash(c["full_hash"])
            except Exception as e:
                print(f"  [{i}/{len(commits)}] {c['hash']} ERROR: {e}")

    if args.dry_run:
        print(f"\n[dry-run] {len(commits)} 件表示（送信なし）")
    else:
        print(f"\n[sync_work_log] 完了: {success}/{len(commits)} 件送信成功")


if __name__ == "__main__":
    main()
