# 🎙️ AI 語音會議助理 (AI Voice Meeting Assistant)

> 讀取本地音訊/影片或透過 **LINE Bot / 桌面 GUI** 傳送語音，利用 **Google Gemini API** 原生音訊處理能力，自動生成完整逐字稿與結構化會議記錄。

---

## 🗂️ 完整專案結構

```
meeting_assistant/
├── meeting_assistant.py    # Phase 0：CLI 快速處理腳本
├── backend/                # Phase 1：FastAPI 後端（核心 API）
│   ├── main.py             #   FastAPI 入口與路由
│   ├── database.py         #   SQLite 資料庫（歷史記錄、支援刪除）
│   ├── tasks.py            #   Gemini AI 背景任務（含長音訊/影片自動切割處理）
│   ├── evidence.py         #   補充資料 / 截圖判讀並追加到會議記錄
│   ├── models.py           #   Pydantic 資料結構
│   └── line_handler.py     #   Phase 3：LINE Bot 訊息處理
├── gui/                    # Phase 2：桌面錄音 GUI
│   ├── app.py              #   Tkinter 主視窗（執行此檔案）
│   ├── recorder.py         #   sounddevice 錄音封裝
│   └── api_client.py       #   後端 HTTP 通訊客戶端
├── static/                 # Phase 4：網頁版前端介面
│   └── index.html          #   提供網頁上傳、歷史瀏覽、原始媒體核對、品質修訂與刪除功能
├── output/                 # AI 生成的 Markdown、原始媒體檔與補充資料附件（自動建立）
│   └── source_audio/       # 已上傳的原始錄音/錄影保留區（沿用舊資料夾名稱）
├── temp/                   # 分段與處理中暫存檔（自動建立）
├── requirements.txt        # 套件相依清單
├── .env                    # 您的私密 API Key（不要上傳 Git！）
└── .env.example            # 環境變數範本
```

---

## 📦 環境建置

### 步驟 1：確認 Python 版本

```bash
python3.13 --version  # 建議 Python 3.13+
```

### 步驟 2：安裝相依套件

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install --upgrade pip setuptools wheel
.venv/bin/python -m pip install -r requirements.txt
```

若要完全重現目前驗證過的 Python 3.13 環境，可改用 `requirements.lock`。

### 步驟 3：設定環境變數

```bash
cp .env.example .env
# 用您慣用的編輯器打開 .env，填入相關金鑰
```

`.env` 內容如下（詳見各章節取得說明）：
```
GEMINI_API_KEY=your_gemini_api_key_here
TRANSCRIPTION_MODEL=gemini-3.1-flash-lite
SUMMARY_MODEL=gemma-4-31b-it
SUMMARY_FALLBACK_MODEL=gemini-3.1-flash-lite
SUMMARY_VERIFIER_MODEL=gemini-3.5-flash
RECORDING_AUDIO_BITRATE=48000
RECORDING_AUDIO_SAMPLE_RATE=24000
RECORDING_AUDIO_CHANNELS=1
RECORDING_COMPACT_AUDIO_BITRATE=32000
RECORDING_COMPACT_AUDIO_SAMPLE_RATE=16000
RECORDING_VIDEO_BITRATE=1000000
RECORDING_VIDEO_FPS=15
LINE_CHANNEL_SECRET=your_line_channel_secret_here
LINE_CHANNEL_ACCESS_TOKEN=your_line_channel_access_token_here
APP_API_KEY=change_me_to_a_long_random_value
MEETING_AUTH_ENABLED=0
MEETING_AUTH_USER_HEADER=X-Meeting-User
MEETING_AUTH_DEFAULT_ROLE=viewer
MAX_UPLOAD_MB=500
CORS_ALLOWED_ORIGINS=http://127.0.0.1:8001,http://localhost:8001
MEETING_ASSISTANT_TRUST_LOCAL_NETWORK=1
MEETING_ASSISTANT_NGROK=1
MEETING_ASSISTANT_NGROK_URL=
MEETING_ASSISTANT_NGROK_API_URL=http://127.0.0.1:4040/api/tunnels
DB_PATH=./meetings.db
MEETING_TEMP_DIR=./temp
MEETING_OUTPUT_DIR=./output
MEETING_SOURCE_AUDIO_DIR=./output/source_audio
MEETING_ATTACHMENT_DIR=./output/attachments
MEETING_BACKUP_DIR=./backups
MEETING_DOCX_TEMPLATE_PATH=./4-QA-005 V01 會議紀錄.docx
DB_BACKUP_KEEP=5
JOB_RETENTION_DAYS=30
JOB_QUEUE_MAX_ATTEMPTS=5
JOB_QUEUE_TRANSIENT_RETRY_DELAY_SECONDS=30
AUDIO_PREPROCESSING=1
AUDIO_MIN_DBFS=-55
AUDIO_NORMALIZE_BELOW_DBFS=-28
SEGMENT_SILENCE_WINDOW_SECONDS=45
SEGMENT_OVERLAP_SECONDS=2
```

安全預設：`/line-webhook` 可公開給 LINE 呼叫；Web 介面與管理 API 允許本機與信任本機網段存取。若要透過 ngrok 或其他公開網路管理，請使用 `APP_API_KEY`。

帳號、角色與稽核紀錄的資料表、helper 與管理 API 已先完成，但 `MEETING_AUTH_ENABLED` 預設為 `0`，目前不會改變既有同網段 / API key 使用方式。啟用前請先建立同仁帳號、角色配置與登入來源；啟用後系統只會從 `app_users` 讀取角色，HTTP header 只提供使用者身分，不可用來授權角色。未啟用時 `/admin/users` 與 `/admin/audit-logs` 會回傳 404。

> 資安提醒：不要提交 `.env`、`meetings.db*`、`temp/`、`output/`、`backups/`、`logs/`、原始錄音、會議記錄或匯出的文件。若金鑰曾暴露，請立即到對應平台輪換 `GEMINI_API_KEY`、`APP_API_KEY`、LINE token 與 ngrok token。

---

## 🧰 驗證與維運

常用的本機驗證命令集中在 `scripts/verify.sh`；Windows 可直接跑 PowerShell 版：

```bash
scripts/verify.sh
```

```powershell
.\scripts\verify.ps1
```

它會執行單元測試、Python 編譯檢查、相依套件檢查，以及網頁 inline JavaScript 語法檢查。若後端已在本機啟動，可再跑前端 smoke：

```bash
BASE_URL=http://127.0.0.1:8001 scripts/smoke_e2e.sh
```

```powershell
$env:BASE_URL = "http://127.0.0.1:8001"
.\scripts\smoke_e2e.ps1
```

若只是想在 Windows 直接啟動臨時後端並跑完 smoke，可用：

```powershell
.\scripts\smoke_with_server.ps1
```

品質警示欄位若懷疑清單、搜尋與詳情顯示不一致，可跑一致性稽核；它也會檢查逐字稿品質警示是否帶有可行動的問題分段或位置。未帶 `--base-url` 時會直接用目前專案的 SQLite 與 FastAPI app 檢查，不需要先啟動後端：

```bash
.venv/bin/python scripts/audit_quality_consistency.py
.venv/bin/python scripts/audit_quality_consistency.py --base-url http://127.0.0.1:8001
```

舊紀錄若已能從 Markdown 推回「第 N 段」問題位置，但資料庫中的 `quality_report_json` 仍是舊式模糊警示，可先 dry-run 檢查會回寫哪些紀錄，再用 `--apply` 寫入結構化問題分段：

```bash
.venv/bin/python scripts/backfill_quality_review_segments.py
.venv/bin/python scripts/backfill_quality_review_segments.py --apply
```

若資料庫已能顯示問題分段，但實體 Markdown 檔案仍缺少「逐字稿品質複核提示」，可用另一個 dry-run 工具檢查並補回檔案提示；這會讓直接開啟或複製舊 Markdown 時也看得到第幾段需要複核：

```bash
.venv/bin/python scripts/backfill_markdown_quality_notes.py
.venv/bin/python scripts/backfill_markdown_quality_notes.py --apply
```

離線會議紀錄品質基準不會呼叫 AI，也不會產生費用：

```bash
.venv/bin/python scripts/run_quality_benchmark.py benchmarks/meeting_quality/cases.example.json --min-score 80
.venv/bin/python scripts/run_quality_benchmark.py --scan-dir output --limit 20 --min-score 75 --format summary
```

第一行適合跑人工確認過的固定案例；第二行會掃描最近產出的 Markdown 會議紀錄，快速找出結構或逐字稿品質疑似退步的檔案。

可調整的維運環境變數：

| 變數 | 預設值 | 用途 |
|------|--------|------|
| `TRANSCRIPTION_MODEL` | `gemini-3.1-flash-lite` | 音訊轉逐字稿使用的模型。若未設定，會沿用舊的 `GEMINI_MODEL` 或預設值。 |
| `SUMMARY_MODEL` | `gemma-4-31b-it` | 根據完整逐字稿產生討論摘要、最終決議與待辦事項的文字模型。 |
| `SUMMARY_FALLBACK_MODEL` | `gemini-3.1-flash-lite` | 摘要模型失敗時自動改用的備援模型，避免整體任務直接失敗。 |
| `SUMMARY_VERIFIER_MODEL` | `gemini-3.5-flash` | 使用「高品質重整」時，第二階段的證據查核模型。 |
| `RECORDING_AUDIO_BITRATE` | `48000` | 標準瀏覽器錄音的 Opus 位元率（bps）。 |
| `RECORDING_AUDIO_SAMPLE_RATE` | `24000` | 標準錄音取樣率（Hz）。 |
| `RECORDING_AUDIO_CHANNELS` | `1` | 錄音聲道數，會議語音建議單聲道。 |
| `RECORDING_COMPACT_AUDIO_BITRATE` | `32000` | 省容量語音 profile 的 Opus 位元率（bps）。 |
| `RECORDING_COMPACT_AUDIO_SAMPLE_RATE` | `16000` | 省容量語音 profile 的取樣率（Hz）。 |
| `RECORDING_VIDEO_BITRATE` | `1000000` | 錄影平衡 profile 的影像位元率（bps），鏡頭錄影與螢幕錄影皆使用此設定。 |
| `RECORDING_VIDEO_FPS` | `15` | 錄影平衡 profile 的目標幀率。 |
| `DB_PATH` | `./meetings.db` | SQLite 資料庫位置，測試或部署時可換到獨立磁碟路徑。 |
| `MEETING_TEMP_DIR` | `./temp` | 分段與處理中的暫存檔資料夾；過期暫存會自動清理。 |
| `MEETING_OUTPUT_DIR` | `./output` | 生成 Markdown 會議記錄的輸出資料夾。 |
| `MEETING_SOURCE_AUDIO_DIR` | `./output/source_audio` | 已上傳原始錄音/錄影的保留資料夾，處理完成後不會自動刪除。 |
| `MEETING_ATTACHMENT_DIR` | `./output/attachments` | 會議補充資料、截圖、PDF、文件的保存位置。 |
| `MEETING_BACKUP_DIR` | `./backups` | 啟動維護時保存 SQLite 備份的位置。 |
| `MEETING_DOCX_TEMPLATE_PATH` | `./4-QA-005 V01 會議紀錄.docx` | Word 匯出使用的本機範本路徑。公司表單範本請保留在本機，不提交到 Git。 |
| `DB_BACKUP_KEEP` | `5` | 保留最近幾份資料庫備份。 |
| `SOURCE_MEDIA_ARCHIVE_RETENTION_DAYS` | `90` | 手動移除原始錄音/錄影後，`backups/source_media_deleted/` 備份保留天數；設為 `0` 可停用自動清理。 |
| `JOB_RETENTION_DAYS` | `30` | 已完成、失敗或取消任務的保留天數。 |
| `JOB_QUEUE_MAX_ATTEMPTS` | `5` | 自動處理任務最多嘗試次數；用於降低 503/暫時性服務忙碌造成的失敗。 |
| `JOB_QUEUE_TRANSIENT_RETRY_DELAY_SECONDS` | `30` | 偵測到 503、429、UNAVAILABLE、timeout 等暫時性錯誤時，下一次重試前等待秒數。 |
| `AUDIO_PREPROCESSING` | `1` | 啟用免費本機音訊預檢；音量過低時建立正規化暫存副本，原始媒體檔不變。 |
| `AUDIO_MIN_DBFS` | `-55` | 低於此平均音量時視為幾乎沒有可辨識聲音，避免浪費模型額度。 |
| `AUDIO_NORMALIZE_BELOW_DBFS` | `-28` | 平均音量低於此值才進行本機音量正規化。 |
| `SEGMENT_SILENCE_WINDOW_SECONDS` | `45` | 在目標切點前後搜尋靜音位置的秒數。 |
| `SEGMENT_OVERLAP_SECONDS` | `2` | 相鄰分段保留的短暫重疊秒數；切點優先位於靜音處。 |
| `MEETING_ASSISTANT_TRUST_LOCAL_NETWORK` | `1` | 是否允許同 Wi-Fi / 信任本機網段直接開 Web 介面；設為 `0` 時手機網址會改用 `api_key`。 |
| `MEETING_AUTH_ENABLED` | `0` | 未來帳號/角色權限開關。預設停用；停用時不會要求登入，也不會改變現有 API key / 同網段行為。 |
| `MEETING_AUTH_USER_HEADER` | `X-Meeting-User` | 未來啟用帳號權限時讀取使用者身分的 HTTP header；角色必須先寫在 `app_users`。 |
| `MEETING_AUTH_DEFAULT_ROLE` | `viewer` | 未來啟用帳號權限時，既有使用者資料缺少角色時的保守預設。 |
| `MEETING_ASSISTANT_NGROK` | `1` | 一鍵啟動是否自動啟動 ngrok；設為 `0` / `false` / `no` 可停用。 |
| `MEETING_ASSISTANT_NGROK_URL` | 空白 | 固定 ngrok 公開 URL，例如 `https://example.ngrok-free.app`。留空時會嘗試沿用 LINE Console 既有 Webhook URL 的網域。 |
| `MEETING_ASSISTANT_NGROK_API_URL` | `http://127.0.0.1:4040/api/tunnels` | ngrok 本機狀態 API；後端 `/metrics` 會讀取它，前端維運面板會顯示 LINE/ngrok 狀態。 |

---

## 🚀 啟動方式

### A. 一鍵啟動（最推薦）

**Mac 使用者**：
直接在 Finder 中雙擊執行 `啟動會議助理.command` 檔案，它會自動啟動後端伺服器並幫您在瀏覽器開啟網頁介面。

**Windows 使用者**：
直接在資料夾中雙擊執行 `啟動會議助理.bat` 檔案，系統會彈出黑色的命令提示字元視窗啟動伺服器，並同樣在瀏覽器為您開啟網頁。

**其他系統或無介面伺服器**：
在終端機輸入以下指令即可啟動：
```bash
.venv/bin/python start.py
```

一鍵啟動也會自動嘗試啟動 ngrok，並在同一個終端機列出 tunnel / LINE webhook test 狀態。網頁介面的「維運狀態」列會顯示 `LINE/ngrok` 是否已連線、目前 `/line-webhook` 公開 URL，以及已保留原始錄音/錄影的檔案數與容量；此容量會一併納入已移除備份，避免備份長期累積卻不易察覺。滑過原始檔欄位可查看目前最大的幾個保留檔、對應會議與未連結檔案數，點擊「原始檔」可開啟維運清單、直接預覽錄音/錄影、開啟或下載保留檔、直接跳到已連結的會議，或手動移除確認不再需要的未連結檔案。清單每次載入最多 500 筆保留檔與 500 筆已移除備份；若仍有更多項目，畫面會提示尚未載入的數量並可按「載入更多」接續查看。手動移除會先搬到 `backups/source_media_deleted/`，不會直接永久刪除；備份旁會保存一個 `.json` metadata，用來保留音訊/錄影類型與原始檔名，讓後續預覽與還原更穩定，維運容量統計也會納入此 metadata。同一個維運視窗也能查看已移除備份、預覽或下載確認內容，並在需要時還原。啟動維護會自動清理超過 `SOURCE_MEDIA_ARCHIVE_RETENTION_DAYS` 的已移除備份，避免備份資料夾無上限膨脹。ngrok log 與 PID 會放在 `logs/ngrok.log`、`logs/ngrok.pid`。

### 手機 / 平板開啟 Web 介面

一鍵啟動時，終端機會列出「手機 / 平板」網址，例如：

```text
手機 / 平板：http://192.168.1.20:8001/history
```

請讓手機與執行後端的 Mac / PC 連到同一個 Wi-Fi，再用手機瀏覽器打開這個網址即可。預設會信任同 Wi-Fi / 本機網段，因此手機不需要輸入 `api_key`。

若使用 ngrok，終端機的「LINE/ngrok 狀態」也會列出「手機 / ngrok 網頁」網址，可在非同 Wi-Fi 環境測試。ngrok 是公開入口，因此該網址仍會帶 `api_key`；請勿公開分享。若外流，請重新啟動以更換臨時 key，或在 `.env` 設定新的 `APP_API_KEY` 後重新啟動。

如果手機仍無法開啟，請先確認：
- 手機與 Mac / PC 在同一個 Wi-Fi，且不是訪客網路或 AP isolation 網路。
- Mac / Windows 防火牆允許 Python / uvicorn 接受區域網路連線。
- 一鍵啟動終端機仍在執行，且沒有顯示 port 被其他程式佔用。
- 若使用 ngrok，網頁介面「LINE/ngrok」需顯示已連線。

### B. 手動啟動 FastAPI 後端與網頁介面（Phase 1 & 4）

```bash
# 後端 API Server 與靜態網頁（Port 8001，避免衝突）
.venv/bin/python -m uvicorn backend.main:app --host 0.0.0.0 --port 8001

# 🌐 開啟網頁版介面 (Phase 4)
open http://127.0.0.1:8001/history

# 🛠️ Swagger UI 測試介面
open http://127.0.0.1:8001/docs
```

### B. 啟動桌面錄音 GUI（Phase 2）

```bash
# 確保後端已在 Port 8001 執行後，再開啟此視窗
.venv/bin/python gui/app.py
```

### C. CLI 快速處理單一音檔（Phase 0）

```bash
.venv/bin/python meeting_assistant.py --audio path/to/meeting.mp3
```

---

## 📋 Phase 3：LINE Bot 設定指南

> **目標**：讓您可以直接在 LINE App 傳送語音，自動獲得會議記錄。

### Step 1：建立 LINE Developers 帳號與 Channel

1. 前往 **[LINE Developers Console](https://developers.line.biz/)**，使用您的個人 LINE 帳號登入。

2. 點擊 **「Create a new provider」**，輸入提供者名稱（例如：`MyCompany`），按「Create」。

3. 在 Provider 頁面，點擊 **「Create a new channel」** → 選擇 **「Messaging API」**。

4. 填寫 Channel 基本資訊：
   - **Channel type**：Messaging API
   - **Provider**：選擇上一步建立的 Provider
   - **Channel name**：例如 `AI 會議助理`
   - **Channel description**：任意填寫
   - **Category / Subcategory**：任意選擇

5. 勾選服務條款，點擊「Create」。

### Step 2：取得 Channel Secret & Access Token

**取得 Channel Secret（頻道密鑰）**：
1. 進入剛建立的 Channel → 點擊 **「Basic settings」** 分頁
2. 往下滑找到 **「Channel secret」** → 點擊「Issue」或複製現有值

**取得 Channel Access Token（頻道存取令牌）**：
1. 進入 Channel → 點擊 **「Messaging API」** 分頁
2. 往下滑找到 **「Channel access token」** → 點擊「Issue」

**填入 `.env`**：
```
LINE_CHANNEL_SECRET=<貼上 Channel secret>
LINE_CHANNEL_ACCESS_TOKEN=<貼上 Channel access token>
```

### Step 3：安裝 ngrok（讓 LINE 能打到您的本機）

> LINE 平台的 Webhook **只接受 HTTPS 公開網址**。ngrok 可將本機 localhost 暫時暴露為公開的 HTTPS URL。

**安裝 ngrok（擇一）**：

```bash
# 方法一：使用 Homebrew（推薦 macOS 使用者）
brew install ngrok

# 方法二：前往 https://ngrok.com/download 下載解壓縮後加入 PATH
```

**免費注冊 ngrok 帳號取得 AuthToken**：
1. 前往 [https://dashboard.ngrok.com/signup](https://dashboard.ngrok.com/signup) 免費註冊
2. 登入後到 **「Your Authtoken」** 頁面複製 token
3. 執行：`ngrok config add-authtoken <YOUR_TOKEN>`

一鍵啟動會自動執行 ngrok；通常不需要另外開一個 ngrok 視窗。建議在 `.env` 設定固定網域，這樣 LINE Console 的 Webhook URL 不必每次重貼：

```bash
MEETING_ASSISTANT_NGROK=1
MEETING_ASSISTANT_NGROK_URL=https://abc123de.ngrok-free.app
```

若 `MEETING_ASSISTANT_NGROK_URL` 留空，一鍵啟動會嘗試用 `LINE_CHANNEL_ACCESS_TOKEN` 讀取 LINE Console 目前設定的 Webhook URL，並沿用該 ngrok 網域啟動 tunnel。

**手動啟動 ngrok（選用）**：

```bash
# 將本機 8001 Port 暴露為公開 HTTPS
ngrok http 8001
```

啟動後會看到類似輸出：
```
Forwarding  https://abc123de.ngrok-free.app -> http://localhost:8001
```

如果沒有固定 ngrok 網域，請複製 `https://abc123de.ngrok-free.app` 這個 URL（每次啟動 ngrok 都可能變化），並更新 LINE Console。

### Step 4：在 LINE 設定 Webhook URL

1. 回到 LINE Developers Console → 您的 Channel → **「Messaging API」** 分頁
2. 找到 **「Webhook URL」** → 點擊「Edit」
3. 貼上：`https://abc123de.ngrok-free.app/line-webhook`（替換為您的 ngrok URL）
4. 點擊「Verify」確認連線成功（應顯示「Success」）
5. 確認 **「Use webhook」** 開關為 **ON**

### Step 5：將 Bot 加為 LINE 好友

1. 在 LINE Developers Console → **「Messaging API」** 分頁
2. 掃描 **「Bot basic ID」** 下方的 QR Code，將 Bot 加為好友

### Step 6：測試

1. 執行一鍵啟動：`.venv/bin/python start.py`，或雙擊 `啟動會議助理.command` / `啟動會議助理.bat`
2. 在終端機確認 `ngrok 已連線` 與 `LINE webhook test：✅ 成功`
3. 在網頁介面確認「LINE/ngrok」顯示 `已連線`
4. 打開 LINE，傳送一則 **語音訊息**，或直接傳送支援格式的音訊 / 影片檔案給 Bot
5. 幾秒後 Bot 回覆「✅ 已收到語音訊息！Gemini 正在分析中...」
6. 處理中可傳送「狀態」、「進度」或 `status` 查詢最近一筆 LINE 任務
7. 約 30~60 秒後，Bot 主動推送摘要、決議與待辦事項；完整逐字稿會保存在 Web 歷史記錄與 Markdown 檔案中 🎉

---

## 📱 LINE Bot 使用限制與系統因應

LINE Messaging API 本身有幾個限制會影響會議助理的使用方式。本專案已在程式中處理可自動補救的限制，但仍建議依下列方式操作。

| 限制 | 對系統的影響 | 目前處理方式 / 建議 |
|------|--------------|---------------------|
| Webhook 必須是公開 HTTPS，且 LINE 會把逾時列為 webhook 錯誤 | 本機服務需透過 ngrok 或正式 HTTPS 網域曝光 | README 的 ngrok 流程即為開發測試用；正式使用建議部署到穩定 HTTPS 主機 |
| Reply Token 只能使用一次，且需很快使用 | AI 分析不可能在 Reply Token 期限內完成 | Webhook 只用 Reply API 快速回「已收到」，實際結果改用 Push Message 傳回 |
| 使用者傳來的音訊 / 檔案只會暫存一段時間，保存時間不保證 | worker 太晚下載可能遇到 404/410，任務會失敗 | 請保持後端與 worker 持續運作；系統收到 LINE 事件後會先排入可靠佇列並盡快下載 |
| 大型音訊 / 影片剛送出時可能尚未完成 LINE 端準備 | 立即呼叫 `Get content` 可能拿到 `202 Accepted` | 系統會輪詢 `/content/transcoding`，等 LINE 回報可下載後再抓檔；可用 `LINE_CONTENT_READY_TIMEOUT_SECONDS` 調整等待上限 |
| 單則文字訊息上限 5000 UTF-16 code units，單次 Push/Reply 最多 5 則 message objects | 長逐字稿可能超過一次 Push request 上限，也會消耗大量 LINE 訊息額度 | LINE 完成通知只推摘要、決議與待辦事項；完整逐字稿保存在 Web 歷史記錄與 Markdown/Word 匯出 |
| Push/API 訊息會受官方帳號方案額度影響 | 長會議紀錄會消耗較多訊息則數 | 台灣官方帳號常見方案額度為輕用量 200 則/月、中用量 3,000 則/月、高用量 6,000 則/月；實際以官方帳號後台為準。若常處理長會議，建議主要從 Web 歷史頁或 Word 匯出取完整紀錄 |
| LINE 檔案訊息需要有可辨識副檔名 | 沒副檔名或不支援格式無法判斷媒體型別 | Bot 支援語音訊息，以及副檔名在本系統支援清單內的檔案，例如 `.mp3`、`.m4a`、`.wav`、`.mp4`、`.mov` |
| Webhook redelivery 可能讓同一事件重送 | 極端情況可能產生重複任務 | 系統會用 LINE `message_id` 擋掉重複排程；仍建議在 LINE Developers Console 開啟 webhook error statistics 觀察錯誤 |

相關官方文件：
- [LINE Messaging API - Get content](https://developers.line.biz/en/reference/messaging-api/#get-content)
- [LINE Messaging API - Send reply message](https://developers.line.biz/en/reference/messaging-api/#send-reply-message)
- [LINE Messaging API - Send push message](https://developers.line.biz/en/reference/messaging-api/#send-push-message)
- [LINE Webhook error statistics](https://developers.line.biz/en/docs/messaging-api/check-webhook-error-statistics/)
- [LINE 官方帳號訊息費用說明](https://tw.linebiz.com/faq/oa-price/message-price-list/)

---

## 📄 輸出格式

生成的 Markdown 檔案包含以下四個區塊：

| 區塊 | 說明 |
|------|------|
| 📋 **會議摘要** | 300 字以內重點概述 |
| ✅ **重要決議** | 明確達成的決議（條列式） |
| 📌 **待辦事項** | 任務 / 負責人 / 期限（表格） |
| 📝 **完整逐字稿** | 區分講者 + 時間戳記 |
| 📎 **補充資料與佐證** | 使用者追加截圖 / 文件後，由 AI 判讀關聯性並補入；此區塊只有在上傳補充資料後出現 |

LINE Bot 完成處理時只會推送前三個區塊與完整檔案位置，避免逐字稿過長造成 LINE 訊息爆量；完整逐字稿請從 Web 歷史記錄、Markdown 檔案或 Word 匯出查看。

長音訊會先切成 10 分鐘分段轉錄，再合併為完整逐字稿。合併時會把分段內的 `[00:00]`、`[09:59]` 等相對時間戳轉成全會議時間，例如第二段會顯示為 `[10:00]`、`[19:59]`。

### Web 品質修訂工具

- 每個逐字稿分段都有「重跑」按鈕。只會重新轉錄指定分段，其餘分段直接沿用原會議紀錄中的逐字稿；舊紀錄也會從分段標題自動重建按鈕。
- 「重整摘要」沿用完整逐字稿，只重新產生摘要、決議與待辦，不會再次轉錄音訊。
- 「高品質重整」會在一般摘要後增加一次第二模型證據查核，因此會多使用一次模型請求；只有手動點選時才會啟用。
- 詳情頁會直接載入保留的原始錄音或錄影；逐字稿中的時間戳可跳回對應媒體時間，也可用「開啟」在新分頁播放或用「下載」保存原始檔。
- 「摘要」只修改摘要、決議與待辦；「逐字稿」只修改完整逐字稿。兩種修改都會在儲存前把完整舊版保存在「版本」中，第一次修改保留的版本即為 AI 原稿。
- 修正逐字稿後，可再按「重整摘要」或「高品質重整」，系統會沿用修正版逐字稿重新產出討論摘要、最終決議與待辦事項。

### 補充資料與截圖佐證

在 Web 歷史記錄打開任一會議後，可點選「補充資料」上傳會議相關檔案；系統會將檔案保存到 `output/attachments/meeting_<會議ID>/`，再請 Gemini 檢視內容、判斷與該會議的關聯性，最後把分析結果追加到同一份 Markdown 的「📎 五、補充資料與佐證」區塊。

目前支援 `.png`、`.jpg`、`.jpeg`、`.webp`、`.pdf`、`.txt`、`.md`、`.csv`、`.docx`。圖片與 PDF 會直接交由 Gemini 視覺/文件能力判讀；文字、Markdown、CSV 與 Word 會先抽取文字再分析。

AI 會輸出「系統判斷」、「擷取重點」、「對會議記錄的影響」、「可能矛盾或待確認」與「來源註記」，並要求明確區分「逐字稿提到」、「補充資料顯示」、「系統推論」、「需人工確認」。第一版不會自動改寫原摘要、決議或待辦事項，而是以佐證區塊保留 AI 建議，方便人工確認後再採用。

### 多語言會議處理

系統會以繁體中文輸出摘要、決議與待辦事項；完整逐字稿則盡量保留實際發言語言：

- 中文國語：以繁體中文轉寫。
- 英文：保留英文原文，較長句子會在同段補繁體中文翻譯。
- 台語：標記為 `[台語]`，以繁體中文做語意轉寫；聽不清楚處會標記 `[台語音訊不清晰]`。
- 人名、公司名、產品名、技術名詞與英文縮寫會盡量保留原文，必要時補中文說明。

---

## ⚠️ 常見問題排除

### Q：後端啟動失敗 `ImportError`
確認已安裝所有套件：`pip3 install -r requirements.txt`

### Q：LINE Webhook Verify 失敗
- 在網頁介面查看「LINE/ngrok」是否為 `已連線`
- 查看 `logs/ngrok.log` 或啟動終端機的 ngrok / LINE webhook test 訊息
- 確認 ngrok URL 未過期，且 LINE Console 的 Webhook URL 是 `<ngrok 公開 URL>/line-webhook`
- 確認後端正在執行（Port 8001）
- 確認 `.env` 中的 `LINE_CHANNEL_SECRET` 正確

### Q：Bot 沒有回應語音訊息
- 確認「Use webhook」已開啟
- 查看終端機後端 LOG 是否有收到 POST `/line-webhook`

### Q：媒體檔上傳逾時
確認網路穩定，或在 `backend/tasks.py` 調大 `MAX_UPLOAD_WAIT_SECONDS`

---

*Powered by Google Gemini API & LINE Messaging API | AI 語音會議助理 v2.0.0*
