#!/usr/bin/env bash
# Turn a 1-hour Threads token for @lovehibou into a 60-day one. For Otto to run
# on his own Mac. The app secret and the short token are read without echo and
# handed to curl through file descriptors, so they never appear on screen, in
# the process list or in shell history, and nothing is written to disk.
# Only the new token and its lifetime are printed, for pasting into the
# HIBOU_THREADS_TOKEN repository secret.
set -euo pipefail
read -r -s -p "Threads 應用程式密鑰（App 後台 → 使用案例 → 存取 Threads API → 設定 → Threads 應用程式密鑰）: " secret; echo
read -r -s -p "短效 token（User Token Generator 用 lovehibou 產生的那串）: " short; echo
response=$(curl -sS -G "https://graph.threads.net/access_token" \
  --data-urlencode "grant_type=th_exchange_token" \
  --data-urlencode client_secret@<(printf '%s' "$secret") \
  --data-urlencode access_token@<(printf '%s' "$short"))
unset secret short
printf '%s' "$response" | python3 -c '
import json, sys
data = json.load(sys.stdin)
if "access_token" not in data:
    sys.exit("Threads 回傳錯誤：" + json.dumps(data.get("error", data), ensure_ascii=False))
days = data.get("expires_in", 0) // 86400
print(f"\n長效 token（約 {days} 天），整串複製到 GitHub repo secret HIBOU_THREADS_TOKEN：\n")
print(data["access_token"])
print("\n記得把 repo variable HIBOU_THREADS_TOKEN_ISSUED 改成今天的日期（YYYY-MM-DD）。\n")
'
