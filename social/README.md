# HiBOU Threads 自動發文（給 Otto 看的說明）

這個資料夾讓 GitHub 用 Threads 官方介面，替 @lovehibou 發文。全程不需要打開 Chrome，所以就算 Chrome 被切到別的帳號，也不會發錯或發不出去。

- 金鑰（token）只放在 GitHub 的加密設定裡，不寫進任何檔案，也不會出現在畫面上。
- 回留言、釘選、在別人的貼文底下留言，還是在 Mac 上用 Chrome 處理，不歸這裡管。

## 一、緊急停止（最重要）

發現發錯、重複發，或帳號怪怪的，照下面做：

1. GitHub 打開 `hibou-social` repo → **Settings** → **Secrets and variables** → **Actions** → **Variables**。
2. 把 `THREADS_PUBLISH_ENABLED` 改成 `false`。
   - 改完立刻生效。之後每次排程都只「模擬」，一則也不會發。
3. 想完全停掉排程：到 **Actions** → 左邊選 **threads-publish** → 右上「⋯」→ **Disable workflow**。

已經發出去的錯誤貼文：程式**不會**幫忙刪。要不要刪，由 Otto 自己在 Threads App 決定。

## 二、每天什麼時候會跑

| 台北時間 | 做什麼 |
|---|---|
| 07:10 | 健康檢查（只讀）：帳號是不是 lovehibou、token 還剩幾天、額度、佇列有沒有卡住 |
| 07:45、12:45、18:45 | 早、中、晚三場的補發（主發由 Mac 上的蹭熱度任務當場觸發） |
| 21:32 | 睡前文 |
| 22:05 | 睡前文補發（21:32 那次被 GitHub 延遲或漏跑時） |

- GitHub 的排程常晚 5 到 30 分鐘，屬於正常。
- 補發不會造成重複，原因見第四節。
- 每次執行完，到 **Actions** 點進那一次，最上面的摘要會寫：帳號、token 剩幾天、額度，以及這次發了什麼（附貼文連結）。

## 三、佇列 `social/queue.json`

只有這個檔案裡 `status` 是 `approved` 的項目會被發出去。每一筆長這樣：

| 欄位 | 意思 |
|---|---|
| `id` | 例如 `2026-10-30-bedtime` |
| `slot` | `morning`（07:30）、`noon`（12:30）、`evening`（18:30）、`bedtime`（21:30）、`test` |
| `publish_at` | 預定時間，一定要寫 `+08:00` |
| `status` | `draft` → `approved` → `publishing` → `published`；或 `failed`、`skipped`；Chrome 時期發過的是 `posted_by_chrome` |
| `media_type` | `IMAGE` 或 `TEXT` |
| `text` | 貼文內容 |
| `image_url`、`image_sha256` | 圖片放在本 repo 的 `social/images/`，網址和檔案指紋要對得上 |
| `topic_tag` | 主題，例如「育兒日常」「帶娃出門崩潰日記」 |
| `self_reply` | 發完後自己回覆的導流文字，例如 `{"text": "延伸看：https://www.ihibou.com/..."}`；不需要就填 `null` |
| `pin_after_post` | 要釘選就填 `true`。API 不能釘選，發完會標 `needs_pin`，要人工去釘 |
| `approved_by`、`approved_at` | 誰在什麼時候核准 |

以下欄位由程式自己填，人不用填：`container_id`、`published_id`、`permalink`、`published_at`、`reply_id`、`run_id`、`error`、`needs_pin`。

**誰可以核准：**
- 一般貼文由 Claude 依授權書第六節核准。
- 測試貼文（`slot: test`），以及切換後第一週的第一批，要 Otto 回「同意」，`approved_by` 寫 `otto`。

## 四、為什麼不會重發

1. 只發 `approved`、而且還在時間窗內的貼文：
   - 睡前文：預定時間後 2 小時內，最晚 23:30。
   - 早、中、晚：預定時間後 90 分鐘內。
   - 過了時間窗就不補發，標成 `skipped`。要改期，由有人在場的工作階段處理。
2. 發文前先讀 @lovehibou 最近 25 篇。只要有一篇的第一行和這則一樣，就當作已經發過，不再發。
3. 真的要發之前，先把狀態改成 `publishing` 並推回 GitHub，推不上去就不發。
4. 看到卡在 `publishing` 的項目，只會提醒人工核對，絕不自動重試。
   - 核對方法：去 @lovehibou 看那則有沒有出現，再把狀態改成 `published` 或 `failed`。
5. 同一天（台北時間）最多 4 則一般貼文，測試貼文不算。

**每次發文前還會先檢查：**
- token 是不是 lovehibou 的，而且帳號 id 也要對得上。不對就整批停止、一則都不發。
- 24 小時發文額度是否還夠（官方上限 250，用到 240 就停）。
- 佇列內容是否通過 `validate.py`，規則如下：
  - 500 字以內，不放 emoji。
  - 連結最多 5 個，只能連到 `www.ihibou.com`。
  - 不出現禁用詞，例如 UPF、抗菌、防蟎、低敏、日本、保證、AI 製作等。
  - 圖片是 JPEG 或 PNG、小於 8 MB、寬 320 到 1440 px。

只要有一筆不合格，整批都不發。

## 五、token 怎麼續期（Otto 本人做）

- token 有效 60 天。剩 10 天時，07:10 的檢查會出現黃色警告；剩 3 天時會變紅色失敗，GitHub 也會寄信通知。
- 續期方法：在自己的 Mac 打開「終端機」，到本 repo 資料夾執行：

```bash
bash social/refresh_token.sh
```

- 依照提示貼上目前的 token。輸入時畫面不會顯示，這是正常的。
- 程式會印出新的 token。把它整串貼到 repo secret `HIBOU_THREADS_TOKEN`，再把 variable `HIBOU_THREADS_TOKEN_ISSUED` 改成今天的日期（`YYYY-MM-DD`）。
- 續期的條件：token 至少用滿 24 小時，而且還沒過期。超過 60 天沒續期，就要回 Meta 後台重新產生，再用 `social/extend_token.sh` 換成 60 天版本。

## 六、GitHub 設定一覽（Otto 本人設定）

| 種類 | 名稱 | 內容 |
|---|---|---|
| Secret | `HIBOU_THREADS_TOKEN` | lovehibou 的 60 天 token |
| Variable | `HIBOU_THREADS_USERNAME` | `lovehibou` |
| Variable | `HIBOU_THREADS_USER_ID` | 第一次健康檢查印出的 id |
| Variable | `HIBOU_THREADS_TOKEN_ISSUED` | token 產生或續期的日期 |
| Variable | `THREADS_PUBLISH_ENABLED` | 總開關：`true` 才會真的發，其他值一律只模擬 |

**名稱刻意和 DeepTutor 的 `THREADS_USER_TOKEN` 不同**，避免貼錯地方。

注意：如果之後替 `main` 開啟分支保護，要讓 GitHub Actions 可以寫入 `main`，不然機器人記不了發文狀態，會變成一則都不發。

## 七、手動執行

到 **Actions** → **threads-publish** → **Run workflow**：

- `mode = check`：只做健康檢查。
- `mode = publish`、`dry_run = true`（預設）：只模擬，列出會發什麼，並確認圖片網址打得開。
- `mode = publish`、`dry_run = false`、`entry_id = 某一筆`：只發那一筆（總開關要是 `true`；Otto 核准的測試貼文例外）。

## 八、給維護的人

- `publish.py`：主程式，只用 Python 標準函式庫。
- `validate.py`：佇列檢查，CI 和發文前都會跑。
- `import_schedule.py`：把 Mac 上 `schedule.json` 的睡前文轉進佇列。預設只印出、不寫入；加 `--write` 才寫。它只讀 `schedule.json`，不會改那個檔。
- `tests/`：用假的 Threads API 測試，不連網路：`python3 -m unittest discover -s social/tests -v`。
- 機器人寫回 `main` 的只有 `social/queue.json` 和 `social/receipts/`。如果剛好有別人同時推送，程式會先抓最新的 `main`，再把自己那一筆狀態重新套上去，不會手動合併 JSON。
- `social/receipts/YYYY-MM.jsonl`：每次發文、回覆、略過、失敗都會加一行，只增不改。
