#!/usr/bin/env bash
# Renew the 60-day Threads token for @lovehibou for another 60 days. For Otto to
# run on his own Mac. The token must be at least 24 hours old and not expired;
# a token left unrenewed past 60 days is dead and has to be generated again.
# The current token is read without echo and handed to curl through a file
# descriptor; nothing is written to disk or shell history. Only the renewed
# token is printed, for pasting into the HIBOU_THREADS_TOKEN repository secret.
set -euo pipefail
read -r -s -p "目前的 HIBOU_THREADS_TOKEN（60 天那串）: " current; echo
response=$(curl -sS -G "https://graph.threads.net/refresh_access_token" \
  --data-urlencode "grant_type=th_refresh_token" \
  --data-urlencode access_token@<(printf '%s' "$current"))
unset current
printf '%s' "$response" | python3 -c '
import json, sys
data = json.load(sys.stdin)
if "access_token" not in data:
    sys.exit("Threads 回傳錯誤：" + json.dumps(data.get("error", data), ensure_ascii=False))
days = data.get("expires_in", 0) // 86400
print(f"\n續期後的 token（約 {days} 天），整串複製到 GitHub repo secret HIBOU_THREADS_TOKEN：\n")
print(data["access_token"])
print("\n記得把 repo variable HIBOU_THREADS_TOKEN_ISSUED 改成今天的日期（YYYY-MM-DD）。\n")
'
