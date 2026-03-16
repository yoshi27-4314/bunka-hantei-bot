#!/usr/bin/env bash
# log_work.sh - Claude Code 作業ログをGoogleスプレッドシートに自動記録する
#
# 使い方:
#   ./scripts/log_work.sh "変更ファイル" "変更内容" ["備考"]
#
# 例:
#   ./scripts/log_work.sh "app.py / CLAUDE.md" "get_bot_role_for_channel()追加" ""

set -euo pipefail

GAS_URL="https://script.google.com/macros/s/AKfycbx9JpYWvi3p0HgA9Bb0RLgEjkgzbF6iJRuAX7Ks2VL3hwIEnpuTR0J1ydtxegGKRXjh/exec"

FILES="${1:-}"
DESCRIPTION="${2:-}"
NOTE="${3:-}"

# 最新コミットのハッシュと日時を取得
COMMIT_HASH=$(git rev-parse --short HEAD 2>/dev/null || echo "")
TIMESTAMP=$(date '+%Y/%m/%d %H:%M')

if [ -z "$FILES" ] || [ -z "$DESCRIPTION" ]; then
  echo "使い方: $0 \"変更ファイル\" \"変更内容\" [\"備考\"]"
  exit 1
fi

PAYLOAD=$(cat <<EOF
{
  "action": "claude_session_log",
  "timestamp": "$TIMESTAMP",
  "author": "浅野儀頼",
  "files": "$FILES",
  "description": "$DESCRIPTION",
  "commit_hash": "$COMMIT_HASH",
  "note": "$NOTE"
}
EOF
)

echo "作業ログを送信中..."
RESPONSE=$(curl -s -L -X POST "$GAS_URL" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD")

echo "レスポンス: $RESPONSE"

if echo "$RESPONSE" | grep -q '"ok":true'; then
  echo "作業ログを記録しました（コミット: $COMMIT_HASH）"
else
  echo "エラーが発生しました"
  exit 1
fi
