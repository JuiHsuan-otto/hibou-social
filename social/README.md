# HiBOU Threads 自動發文（給 Otto 看的說明）

這個資料夾讓 GitHub 用 Threads 官方介面，替 @lovehibou 發文。全程不需要打開 Chrome，所以就算 Chrome 被切到別的帳號，也不會發錯或發不出去。

- 金鑰（token）只放在 GitHub 的加密設定裡，不寫進任何檔案，也不會出現在畫面上。
- 回留言、在別人的貼文底下留言，由 `threads-engage` 處理（見第九節）。釘選還是要人工在 App 裡做。

## 一、緊急停止（最重要）

發現發錯、重複發，或帳號怪怪的，照下面做：

1. GitHub 打開 `hibou-social` repo → **Settings** → **Secrets and variables** → **Actions** → **Variables**。
2. 把 `THREADS_PUBLISH_ENABLED` 改成 `false`。
   - 改完立刻生效。之後每次排程都只「模擬」，一則也不會發。
3. 想完全停掉排程：到 **Actions** → 左邊選 **threads-publish** → 右上「⋯」→ **Disable workflow**。

已經發出去的錯誤貼文：程式**不會**幫忙刪。要不要刪，由 Otto 自己在 Threads App 決定。

## 二、每天什麼時候會跑

| 台北時間 | UTC | 做什麼 |
|---|---|---|
| 07:10 | 23:10 | 健康檢查（只讀）：帳號是不是 lovehibou、token 還剩幾天、額度、佇列有沒有卡住 |
| 07:45 | 23:45 | 早上補發 |
| 12:45 | 04:45 | 中午補發（含 Facebook 12:20、Instagram 12:25） |
| 18:10、18:25、18:45、18:50、19:20 | 10:10、10:25、10:45、10:50、11:20 | 18:05 文章導流。90 分鐘窗到 19:35，所以這幾班都還趕得上。刻意避開 :00 和 :30 |
| 19:50 | 11:50 | 只補 18:05 文章的晚窗（3 小時，到 21:05）。20:00 的貼文這時還沒到 |
| 20:10、20:25、20:40、21:05 | 12:10、12:25、12:40、13:05 | 20:00 的 evening 貼文。90 分鐘窗到 21:30。18:05 文章若還沒發，20:10 和 20:40 仍在 3 小時晚窗內 |
| 21:32 | 13:32 | 睡前文 |
| 22:05 | 14:05 | 睡前文補發。文章導流晚上 22:30 之後不發 |

- GitHub 的排程常晚 5 到 30 分鐘，屬於正常。
- 補發不會造成重複，原因見第四節。
- 每次執行完，到 **Actions** 點進那一次，最上面的摘要會寫：帳號、token 剩幾天、額度，以及這次發了什麼（附貼文連結）。

## 三、佇列 `social/queue.json`

只有這個檔案裡 `status` 是 `approved` 的項目會被發出去。每一筆長這樣：

| 欄位 | 意思 |
|---|---|
| `id` | 例如 `2026-10-30-bedtime`。文章草稿會用 `2026-11-25-facebook-文章slug` 這種 id |
| `platform` | 不填就是 Threads。`facebook` 是粉專連結貼文，`instagram` 是單圖或輪播 |
| `slot` | `morning`（07:30）、`noon`（12:30）、`evening`（18:30）、`bedtime`（21:30）、`test` |
| `publish_at` | 預定時間，一定要寫 `+08:00` |
| `status` | `draft` → `approved` → `publishing` → `published`；或 `failed`、`skipped`；Chrome 時期發過的是 `posted_by_chrome` |
| `media_type` | Threads：`TEXT`、`IMAGE`、`VIDEO`。Facebook：`LINK`。Instagram：`IMAGE` 或 `CAROUSEL` |
| `text` | 貼文內容。Facebook 與 Instagram 另有文案規則，見第十節 |
| `image_url`、`image_sha256` | Threads 圖片放在本 repo 的 `social/images/`，網址和檔案指紋要對得上。Instagram 單圖改用官網上的 JPEG 或 PNG，不填指紋 |
| `image_urls` | Instagram 輪播，2 到 10 張官網上的 JPEG 或 PNG |
| `video_url` | 影片網址。必須是本 repo `main` 上 `social/videos/` 的 raw 連結，且以 `.mp4` 結尾。跟圖片一樣指到 `main`，所以分支合併前 Threads 抓不到檔。不填 sha256 |
| `link` | Threads 文字貼文的連結預覽，或 Facebook 要打開的文章網址。Facebook 只能填 `https://www.ihibou.com/articles/` 或 `/journals/`，而且不能帶網址參數 |
| `bio_link` | 只給 Instagram。發文前要手動放進簡介的文章網址，程式不會把它寫進說明 |
| `topic_tag` | Threads 主題，例如「育兒日常」「帶娃出門崩潰日記」。Facebook 與 Instagram 不要填 |
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
   - 文章導流（`source` 以 `article:` 開頭）：可以晚到預定時間後 3 小時，但台北時間 22:30 之後不發。18:05 的文章因此最晚到 21:05。已經過了原本的 90 分鐘、又離同一平台另一則不到 3 小時的，這次先不發，狀態維持 `approved`，留給下一班；窗關了才標成 `skipped`。
   - 過了時間窗就不補發，標成 `skipped`。要改期，由有人在場的工作階段處理。
2. 發文前先讀 @lovehibou 最近 25 篇。只要有一篇的第一行和這則一樣，就當作已經發過，不再發。
3. 真的要發之前，先把狀態改成 `publishing` 並推回 GitHub，推不上去就不發。
4. 看到卡在 `publishing` 的項目，只會提醒人工核對，絕不自動重試。
   - 核對方法：去 @lovehibou 看那則有沒有出現，再把狀態改成 `published` 或 `failed`。
5. 同一天（台北時間）最多 4 則一般 Threads 貼文，測試貼文不算。Facebook 與 Instagram 也不算進這 4 則。

**每次發文前還會先檢查：**
- token 是不是 lovehibou 的，而且帳號 id 也要對得上。不對就整批停止、一則都不發。
- 24 小時發文額度是否還夠（官方上限 250，用到 240 就停）。
- 佇列內容是否通過 `validate.py`，規則如下：
  - 500 字以內，不放 emoji。
  - 連結最多 5 個，只能連到 `www.ihibou.com`。
  - 不出現禁用詞，例如 UPF、抗菌、防蟎、低敏、日本、保證、AI 製作等。
  - 圖片是 JPEG 或 PNG、小於 8 MB、寬 320 到 1440 px。
  - 影片：`publish.py` 用 `media_type=VIDEO` 加公開的 `video_url` 建容器（跟圖片的 `image_url` 同一套），等容器 `FINISHED` 再發，發完一樣可以自己回覆。Threads 官方上限是 MP4/MOV、1 GB、長度大於 0 秒且最長 5 分鐘、寬最多 1920、長寬比 0.01:1 到 10:1（建議 9:16）、H.264 或 HEVC、AAC、moov 要在檔案前面。本機驗證只擋：網址不在 `social/videos/`、不是 `.mp4`、超過 1 GB、檔頭不是 MP4、或 `mdat` 排在 `moov` 前面。編碼、影格率、秒數不在這裡解析。檔案還沒推進 repo 時，只檢查網址，不會造一個假的 checksum。影片容器最多輪詢 5 分鐘。

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
| Variable | `FB_PUBLISH_ENABLED` | Facebook 粉專總開關：`true` 才真的發，其他值只模擬 |
| Variable | `IG_PUBLISH_ENABLED` | Instagram 總開關：`true` 才真的發，其他值只模擬 |
| Secret | `HIBOU_FB_PAGE_TOKEN` | 粉專的長期 Page token。沒有這把，Facebook 與 Instagram 略過，Threads 照常 |
| Secret | `HIBOU_IG_USER_ID` | Instagram 專業帳號的 id。沒有的話只略過 Instagram |
| Variable | `HIBOU_FB_PAGE_ID` | 選用。有填的話，token 對到的粉專 id 必須一樣，否則這次不發 |
| Variable | `THREADS_ENGAGE_ENABLED` | 互動總開關：`true` 才會真的回覆或留言，其他值一律只模擬 |
| Variable | `THREADS_ENGAGE_DISCOVERY` | 關鍵字留言：`false` 強制關閉；`true` 就算權限清單讀不到也會試；不設的話，token 有 `threads_keyword_search` 才開 |
| Secret | `GEMINI_API_KEY` | 選用。有這把金鑰時，一般回覆和關鍵字留言用 Gemini 寫。沒有就用固定句型。也可以改放 `GOOGLE_API_KEY` |
| Variable | `HIBOU_ENGAGE_LLM_PROVIDER` | 選用。`gemini`、`template` 或留空（有金鑰就用 Gemini） |
| Variable | `HIBOU_ENGAGE_GEMINI_MODEL` | 選用。預設 `gemini-2.5-flash` |

**名稱刻意和 DeepTutor 的 `THREADS_USER_TOKEN` 不同**，避免貼錯地方。

注意：如果之後替 `main` 開啟分支保護，要讓 GitHub Actions 可以寫入 `main`，不然機器人記不了發文狀態，會變成一則都不發。

## 七、手動執行

到 **Actions** → **threads-publish** → **Run workflow**：

- `mode = check`：只做健康檢查。
- `mode = publish`、`dry_run = true`（預設）：只模擬，列出會發什麼，並確認圖片網址打得開。
- `mode = publish`、`dry_run = false`、`entry_id = 某一筆`：只發那一筆（總開關要是 `true`；Otto 核准的測試貼文例外）。

## 八、給維護的人

- `publish.py`：主程式，只用 Python 標準函式庫。Threads 發完後才處理 Facebook 與 Instagram。
- `meta_publish.py`：粉專連結貼文、Instagram 單圖與輪播。token 只從環境變數進來。
- `article_queue.py`：讀一篇活著的文章，產生三個平台的草稿。
- `validate.py`：佇列檢查，CI 和發文前都會跑。
- `import_schedule.py`：把 Mac 上 `schedule.json` 的睡前文轉進佇列。預設只印出、不寫入；加 `--write` 才寫。它只讀 `schedule.json`，不會改那個檔。
- `tests/`：用假的 Threads API 測試，不連網路：`python3 -m unittest discover -s social/tests -v`。
- 發文流程寫回 `main` 的是 `social/queue.json` 和 `social/receipts/`。如果剛好有別人同時推送，程式會先抓最新的 `main`，再把自己那一筆狀態重新套上去，不會手動合併 JSON。
- 互動流程另外寫 `social/engagement/YYYY-MM-DD.jsonl`。Insights 寫 `social/metrics/`。
- `social/receipts/YYYY-MM.jsonl`：每次發文、回覆、略過、失敗都會加一行，只增不改。
- `engage.py`、`engage_copy.py`：回覆與關鍵字留言。規則與開關見第九節。
- `social/engagement/YYYY-MM-DD.jsonl`：互動紀錄，只增不改。dry-run 不寫這個檔。這個 repo 是公開的，所以紀錄只留貼文編號、時間、我們回的話和我們自己的貼文連結；不記對方帳號、對方貼文連結或分類（客訴、醫療等）。「同一人一天最多 2 則」用 `author_key`（以 token 為鑰的雜湊）判斷，無法反推帳號。Actions 輸出裡對方帳號一律遮成 `@m***`，需要人工跟進時附的是我們自己的貼文或回覆連結。

## 九、回覆與關鍵字留言（`threads-engage`）

這條流程做兩件事：

1. **回覆（排程一定會跑）**：最近 14 天 @lovehibou 貼文底下的新留言、別人回我們留在別處的留言，以及提及 @lovehibou 的貼文。同一則不會回第二次。每一輪最多回 12 則，剩下的下一輪再處理。
2. **關鍵字留言（預設關，token 有權限才開）**：每一輪換兩個跟 HiBOU 有關的主題（彌月送禮、新生兒準備、推車出門、寶寶睡眠、換季保暖、坐月子日常、送禮給朋友的寶寶），搜尋近期的文字貼文，挑已經有人在聊、而且接得上的留言。自己的貼文不留。

分類：

| 對方在說什麼 | 我們怎麼做 |
|---|---|
| 一般聊天 | 接住對方講的一個具體細節，用台灣口語回 1 到 2 句 |
| 價格、尺寸、怎麼買、庫存、交期、刺繡 | 請對方私訊或 LINE，不在留言裡報價、不講庫存或交期 |
| 送禮日、趕不趕得及 | 原文就是「有送禮日的話，先用 LINE 問我們」 |
| 抱怨、安全、醫療 | 短短一句請對方私訊，並在這次執行標成需要人工跟進 |
| 垃圾、詐騙、辱罵 | 不回，寫進紀錄 |
| 傷慟 | 不自動回，標成需要人工跟進 |

關鍵字留言會跳過：政治、爭議、醫療求助、傷慟、抱怨別人、商店或業配、離題、以照片或影片為主的貼文（無法確認是不是未成年人）、抽獎、詐騙。每輪最多 8 則，每天最多 40 則，同一人每天最多 2 則，同一則貼文只留一次。有人已經在底下說話的近期貼文會先留。

共同規則：每則至少間隔 60 秒；避開台北時間 07:30、12:30、18:30、21:30 前後 5 分鐘。碰到速率限制、垃圾訊息警告、留言被隱藏或社群政策錯誤，這輪立刻停止。文案在送出前會擋掉網址、價格、療效、庫存交期、日本製／日本設計／日本熱銷、hashtag、客服腔，以及津貼、補助、戶口、保險、稅或醫療的天數、金額和期限。碰到這些，只請對方跟戶政或官網確認，不講規則。

權限不夠時這輪只會說明缺什麼，不會把那些留言記成已回覆。需要的權限、重新產生 token、打開正式送出的步驟寫在這個流程的 pull request 說明裡。

**什麼時候會跑（台北時間）：** 09:00、11:00、14:00、16:00、19:00、22:00。GitHub 可能晚 5 到 30 分鐘。

**總開關沒打開時，每一次都只模擬。** 模擬會把預計送出的句子寫進 Actions 摘要和 artifact，不會寫進 `social/engagement/`，所以之後打開開關仍會真的回。

手動執行：Actions → **threads-engage** → **Run workflow**。`dry_run` 預設是開的。要真的送出，總開關要是 `true`，而且 `dry_run` 要關掉，並且從 `main` 跑。

緊急停止：把 `THREADS_ENGAGE_ENABLED` 改成 `false`，或到 Actions 把 **threads-engage** 停用。已經送出的留言不會自動刪。

## 十、Facebook 粉專與 Instagram（文章導流）

Threads 的排程沒有改。同一支 `threads-publish` 在 Threads 處理完之後，才看佇列裡 `platform` 是 `facebook` 或 `instagram` 的項目。開關預設是關的，關著就只模擬。

文章進佇列用這個指令（先印出、不寫入；確認沒問題再加 `--write`）：

```bash
python3 social/article_queue.py https://www.ihibou.com/articles/某一篇
python3 social/article_queue.py --file urls.txt --write
```

它會自己打開那一頁，確認是 HTTP 200，讀標題、說明和 `og:image`，然後寫三則 `draft`：

| 平台 | 時間（台北） | 既有排程會在什麼時候送出 | 貼法 |
|---|---|---|---|
| Threads | 07:05 | 07:45 那班 | 文字，附上文章網址 |
| Facebook | 12:20 | 12:45 那班 | 連結貼文，點下去就是那篇文章 |
| Instagram | 12:25 | 12:45 那班 | 單圖或輪播。說明不能放網址，所以寫「連結放在簡介」 |

這三個時間刻意不是 07:30、12:30、18:30、21:30，也不是同一分鐘。Facebook、Instagram 用的是 HiBOU 發文規則的 12:20 文章時段，只排**週一、三、五**（週二、四留給 Business Suite 的育兒文接力），跟 16:30／20:30 短片、20:35 限動都隔 3 小時以上。已經有別的 12:20 貼文的日子用 `--busy-dates 2026-12-11,2026-12-18` 跳過。現有的 12:00、20:00 Threads 貼文不會被改到。一天若已經有 4 則 Threads，文章那則會改排到下一天。這 4 則只算 Threads，粉專和 Instagram 不算。草稿不會自己變核准，要有人把 `status` 改成 `approved` 並填 `approved_by`、`approved_at` 才會進發送流程。

Instagram 的說明裡沒有連結。`bio_link` 記的是這篇文章的網址，發出去之前要先到 Instagram 把簡介連結改成這個網址。模擬時也會再提醒一次。

**文案不能出現：** 內部 hashtag、AI 或製作來源的說法、價格、庫存數字、交期、醫療功效、日本製／日本設計／日本熱銷、「您」、條列。提到送禮日，只能用「有送禮日的話，先用 LINE 問我們」。語氣用台灣口語。三個平台的句子不會是同一段。

只要佇列裡有一則草稿或已核准的項目沒過 `validate.py`，Threads 也會整批不發。這是原來的規則，Facebook 與 Instagram 也算在裡面。

**缺金鑰時：** 沒有 `HIBOU_FB_PAGE_TOKEN`，Facebook 和 Instagram 這次略過，佇列不改，Threads 照發。沒有 `HIBOU_IG_USER_ID` 時只略過 Instagram。token 無效或粉專 id 對不上，同樣不改那些項目的狀態（粉專 id 對不上時，這次執行會標失敗，提醒 Otto 看設定）。

**什麼時候才會真的貼出去：** `FB_PUBLISH_ENABLED` 或 `IG_PUBLISH_ENABLED` 是 `true`，而且這次是從 `main` 跑、不是手動的 dry-run。兩個開關分開。緊急停止就把對應的變數改成 `false`。已經貼出去的，程式不會刪。

Threads 的 token 如果無效，整次執行還是會先停，Facebook 與 Instagram 也不會發。這點跟原來的 Threads 檢查一樣。

### Otto 要在 Meta 設定的權限

App 要能管 HiBOU 的粉專，而且 Instagram 是專業帳號、已經連到這個粉專。App 要切到上線（Live）；還在開發模式時，只有 App 角色能發。

向使用者要這些權限：

- `pages_show_list`：列出這個人管的粉專
- `pages_read_engagement`：讀粉專貼文，發文前對第一行，避免重發
- `pages_manage_posts`：在粉專發連結貼文
- `instagram_basic`：讀 Instagram 專業帳號
- `instagram_content_publish`：發單圖與輪播

粉專如果放在企業管理平台（Business Manager）裡，`/me/accounts` 回傳是空的，再補 `business_management`。

### 長期粉專 token 怎麼換（Otto 本人做）

不要把 token 貼進 repo、PR 或聊天紀錄。下面的網址只是格式，花括號要換成自己的值，而且只在自己的電腦上打。

1. 用 Graph API Explorer 或 Facebook 登入，拿到短效使用者 token（大約 1 小時），範圍要包含上面的權限。
2. 換成約 60 天的使用者 token：

```text
GET https://graph.facebook.com/v21.0/oauth/access_token
  ?grant_type=fb_exchange_token
  &client_id={app-id}
  &client_secret={app-secret}
  &fb_exchange_token={短效使用者 token}
```

3. 用這個長效使用者 token 問粉專。在 Graph API Explorer 右上角選這個 token 再送出，不要把 token 接在網址後面貼到別處。回傳裡那個粉專自己的 token 就是長期粉專 token（由長效使用者 token 換來的粉專 token 不會自己過期；人改密碼、移除 App 或權限被收回時才會失效）：

```text
GET https://graph.facebook.com/v21.0/me/accounts
```

4. 把那串粉專 token 貼到 repo secret `HIBOU_FB_PAGE_TOKEN`。粉專的 `id` 可以選擇填進 variable `HIBOU_FB_PAGE_ID`。
5. 問 Instagram 專業帳號 id，貼到 secret `HIBOU_IG_USER_ID`：

```text
GET https://graph.facebook.com/v21.0/{page-id}?fields=instagram_business_account
```

回傳的 `instagram_business_account.id` 就是要填的 id。程式日誌不會印出這組 id，也不會印出 token。

發文打的是 `https://graph.facebook.com/v21.0`。粉專是 `POST /{page-id}/feed`（`message` 加 `link`）。Instagram 是先 `POST /{ig-user-id}/media` 建容器，等 `status_code` 變成 `FINISHED`，再 `POST /{ig-user-id}/media_publish`。輪播是先建每一張圖（`is_carousel_item=true`），再把那些 id 放進一則 `CAROUSEL`。Instagram 每 24 小時大約 50 則，用到 40 這次就先停。

