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
TRANSCRIPTION_RECOVERY_MODEL=gemini-3.5-flash
GENAI_HTTP_TIMEOUT_SECONDS=180
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
JOB_QUEUE_TRANSIENT_RETRY_BACKOFF_MULTIPLIER=2
JOB_QUEUE_TRANSIENT_RETRY_MAX_DELAY_SECONDS=300
AUDIO_PREPROCESSING=1
AUDIO_MIN_DBFS=-55
AUDIO_NORMALIZE_BELOW_DBFS=-28
AUDIO_INITIAL_SPEECH_FOCUS=1
AUDIO_INITIAL_SPEECH_FOCUS_MIN_ACTIVE_RATIO=0.55
AUDIO_INITIAL_SPEECH_FOCUS_CLIP_DBFS=-0.1
SPEECH_FOCUS_LOSSLESS_UPLOAD=1
SEGMENT_SILENCE_WINDOW_SECONDS=45
SEGMENT_OVERLAP_SECONDS=2
TRANSCRIPT_INTRA_SEGMENT_TIMESTAMP_REGRESSION_TOLERANCE_SECONDS=15
INITIAL_DENSE_AUDIO_SPLIT=1
INITIAL_DENSE_AUDIO_SPLIT_MINUTES=5
INITIAL_CLIPPED_DENSE_AUDIO_SPLIT=1
INITIAL_CLIPPED_DENSE_AUDIO_SPLIT_MINUTES=3
INITIAL_CLIPPED_DENSE_AUDIO_CLIP_DBFS=-0.1
INITIAL_DENSE_AUDIO_MIN_ACTIVE_SECONDS=180
INITIAL_DENSE_AUDIO_MIN_ACTIVE_RATIO=0.55
INITIAL_DENSE_AUDIO_SEGMENT_OVERLAP_SECONDS=5
SEGMENT_OVERLAP_LEADING_FILLER_DEDUPLICATION=1
RECOVERY_SUBSEGMENT_OVERLAP_SECONDS=2
RECOVERY_SHORT_SUBSEGMENT_MAX_SECONDS=30
RECOVERY_SHORT_SUBSEGMENT_OVERLAP_SECONDS=4
TRANSCRIPT_SPEECH_GAP_VALIDATION=1
TRANSCRIPT_SPEECH_GAP_SECONDS=60
TRANSCRIPT_SPEECH_GAP_MIN_ACTIVE_SECONDS=12
TRANSCRIPT_SPEECH_GAP_MIN_ACTIVE_RATIO=0.25
TRANSCRIPT_SPEECH_DENSITY_VALIDATION=1
TRANSCRIPT_SPEECH_DENSITY_MIN_ACTIVE_SECONDS=90
TRANSCRIPT_SPEECH_DENSITY_SHORT_SEGMENT_MIN_ACTIVE_SECONDS=15
TRANSCRIPT_SPEECH_DENSITY_MIN_ACTIVE_RATIO=0.45
TRANSCRIPT_SPEECH_DENSITY_MIN_CHARS_PER_ACTIVE_SECOND=2.5
TRANSCRIPT_LOCAL_DENSITY_VALIDATION=1
TRANSCRIPT_LOCAL_DENSITY_WINDOW_SECONDS=90
TRANSCRIPT_LOCAL_DENSITY_STEP_SECONDS=45
TRANSCRIPT_LOCAL_DENSITY_MIN_ACTIVE_SECONDS=35
TRANSCRIPT_LOCAL_DENSITY_MIN_ACTIVE_RATIO=0.45
TRANSCRIPT_LOCAL_DENSITY_MIN_CHARS_PER_ACTIVE_SECOND=1.5
TRANSCRIPT_LOCAL_DENSITY_MAX_RANGES=4
TRANSCRIPT_SPEECH_GAP_MAX_RANGES=6
TRANSCRIPT_REPAIR_CONTEXT_SECONDS=6
TRANSCRIPT_REPAIR_DIRECT_SPLIT_SECONDS=180
TRANSCRIPT_CONFIRMED_GAP_RECOVERY_SECONDS=180
TRANSCRIPT_MULTI_GAP_RECOVERY_SECONDS=120
TRANSCRIPT_LOCAL_REPAIR_RECOVERY_SECONDS=60
TRANSCRIPT_SEVERE_LOCAL_DENSITY_MAX_CHARS_PER_ACTIVE_SECOND=0.5
TRANSCRIPT_SEVERE_LOCAL_DENSITY_RECOVERY_SECONDS=30
TRANSCRIPT_CRITICAL_RERUN_ESCALATION=1
TRANSCRIPT_CRITICAL_SUSTAINED_GAP_SECONDS=60
TRANSCRIPT_CRITICAL_REPETITION_MIN_TURNS=8
TRANSCRIPT_SHORT_CYCLE_DUPLICATE_VALIDATION=1
TRANSCRIPT_SHORT_CYCLE_DUPLICATE_MIN_CHARS=20
TRANSCRIPT_SHORT_CYCLE_DUPLICATE_MIN_SIMILARITY=0.88
TRANSCRIPT_SHORT_CYCLE_DUPLICATE_MIN_TURNS=3
TRANSCRIPT_SHORT_CYCLE_DUPLICATE_WINDOW_TURNS=4
TRANSCRIPT_SHORT_CYCLE_DUPLICATE_MAX_SPAN_SECONDS=30
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

它會執行單元測試、Python 編譯檢查、相依套件檢查、網頁 inline JavaScript 語法檢查與不安全分段快取檢查。若偵測到舊快取有時間戳越界或重複幻覺等問題，先以可復原方式隔離，再重新驗證：

```powershell
.\.venv\Scripts\python.exe scripts\prune_bad_segment_cache.py --apply
```

隔離檔與 `manifest.json` 會保留在 `output/segment_cache_quarantine/`，不會永久刪除。若後端已在本機啟動，可再跑前端 smoke：

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
| `TRANSCRIPTION_RECOVERY_MODEL` | `gemini-3.5-flash` | 第一轉錄模型完成局部/小段補救後仍有時間缺口、文字密度偏低、數列延伸或長重複迴圈等可驗證異常時，或遇到 `429`、`5xx`、逾時等暫時性上游錯誤時使用的備援轉錄模型。若此模型留下較佳但未完成的候選稿，後續佇列重試會以目前設定的同一補救模型接續剩餘區間，不會重做已證實較弱的主模型嘗試。設為與 `TRANSCRIPTION_MODEL` 相同即可停用。 |
| `TRANSCRIPTION_FULL_RERUN_MODEL` | `gemini-3.5-flash` | 手動「完整重跑」指定分段時使用的模型；按整份「重跑」時，也只會套用到既有品質報告已用音訊證實有重大異常的時間區間。即使新版依語音密度重新切段，系統也會按原始媒體時間重新定位，不會誤用舊分段編號；原本的音訊品質證據會隨佇列保留。預設沿用補救模型，通常以約 60 秒小段轉錄；若本機已確認文字量極端偏低，會直接縮為 30 秒，避免先進行一次已知較不穩定的嘗試。設為與 `TRANSCRIPTION_MODEL` 相同可維持原模型。 |
| `TRANSCRIPT_SEMANTIC_REVIEW_MODEL` | `gemma-4-31b-it` | 手動「語意檢核」使用的文字模型；只標示高度明確的語句失真時間位置，不會改寫逐字稿、摘要或待辦事項。 |
| `TRANSCRIPT_SEMANTIC_REVIEW_FALLBACK_MODEL` | `gemini-3.5-flash` | 語意檢核主模型暫時失敗時使用的備援模型；只在主模型失敗時呼叫。 |
| `GENAI_HTTP_TIMEOUT_SECONDS` | `180` | Gemini 單次 HTTP 請求的逾時秒數，涵蓋上傳、轉錄與摘要；逾時後交由既有任務佇列重試，避免工作永久停在處理中。 |
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
| `JOB_QUEUE_TRANSIENT_RETRY_DELAY_SECONDS` | `30` | 偵測到 503、429、UNAVAILABLE、timeout 等暫時性錯誤時，第一次重試前等待秒數。 |
| `JOB_QUEUE_TRANSIENT_RETRY_BACKOFF_MULTIPLIER` | `2` | 暫時性錯誤每次重試的等待倍數；預設依序為 30、60、120 秒。 |
| `JOB_QUEUE_TRANSIENT_RETRY_MAX_DELAY_SECONDS` | `300` | 暫時性錯誤單次等待的上限秒數，避免等待時間無限增加。 |
| `AUDIO_PREPROCESSING` | `1` | 啟用免費本機音訊預檢；音量過低時建立正規化暫存副本，爆音高密度來源可建立語音聚焦暫存副本，原始媒體檔不變。 |
| `AUDIO_MIN_DBFS` | `-55` | 低於此平均音量時視為幾乎沒有可辨識聲音，避免浪費模型額度。 |
| `AUDIO_NORMALIZE_BELOW_DBFS` | `-28` | 平均音量低於此值才進行本機音量正規化。 |
| `AUDIO_INITIAL_SPEECH_FOCUS` | `1` | 首次轉錄若同時有爆音、高語音密度與大動態範圍，建立本機 24 kHz 無損 FLAC 語音聚焦副本；一般錄音與原始媒體檔不受影響。 |
| `AUDIO_INITIAL_SPEECH_FOCUS_MIN_ACTIVE_RATIO` | `0.55` | 首次語音聚焦所需的最小有效語音比例；可避免在大多安靜或間歇性錄音上做不必要的有損暫存處理。 |
| `AUDIO_INITIAL_SPEECH_FOCUS_CLIP_DBFS` | `-0.1` | 首次轉錄只有峰值接近 `0 dBFS` 的實際爆音才建立語音聚焦副本，避免峰值正常的錄音被不必要地重編碼；已確認異常後的補救仍採用較寬的 `RECOVERY_SPEECH_FOCUS_CLIP_DBFS`。 |
| `SPEECH_FOCUS_LOSSLESS_UPLOAD` | `1` | 將系統建立的語音聚焦 FLAC 分段及其後續重跑／局部補救子段直接無損上傳，避免在品質補救前再次壓成 MP3；僅影響已判定高風險的爆音或品質異常分段，一般錄音維持較小 MP3。 |
| `SPEECH_FOCUS_TIMEOUT_SECONDS` | `180` | 本機 `ffmpeg` 建立語音聚焦 FLAC 的最長處理秒數（90-600）；較長錄音不會因舊版 90 秒時限而過早回退原音。 |
| `RECOVERY_SPEECH_FOCUS` | `1` | 僅在已被品質檢核標示、需要重跑的分段中建立 24 kHz 無損 FLAC 語音聚焦暫存副本；避免重複 MP3 壓縮與 `loudnorm` 的過度升頻，原始媒體檔不變。 |
| `RECOVERY_SPEECH_FOCUS_CLIP_DBFS` | `-0.5` | 補救時偵測到接近滿刻度峰值才考慮語音聚焦處理。 |
| `RECOVERY_SPEECH_FOCUS_MIN_DYNAMIC_RANGE_DB` | `14` | 峰值與平均音量至少相差此值才套用動態壓縮，避免不必要處理正常錄音。 |
| `RECOVERY_SPEECH_FOCUS_TARGET_LUFS` | `-19` | 語音聚焦副本的目標響度；搭配壓縮後拉回安靜發言，僅影響問題分段的暫存副本。 |
| `RECOVERY_SPEECH_FOCUS_TRUE_PEAK_DB` | `-1.5` | 語音聚焦副本的真實峰值上限，避免壓縮後再度爆音。 |
| `SEGMENT_SILENCE_WINDOW_SECONDS` | `45` | 在目標切點前後搜尋靜音位置的秒數。 |
| `SEGMENT_OVERLAP_SECONDS` | `2` | 相鄰分段保留的短暫重疊秒數（0-10 秒）；切點優先位於靜音處。 |
| `TRANSCRIPT_CROSS_SEGMENT_TIMESTAMP_REGRESSION_TOLERANCE_SECONDS` | `10` | 跨分段時間戳允許的最大倒退秒數；超過時會阻擋摘要，避免時間軸錯序的逐字稿產出會議結論。 |
| `TRANSCRIPT_INTRA_SEGMENT_TIMESTAMP_REGRESSION_TOLERANCE_SECONDS` | `15` | 單一分段內時間碼可容許的小幅倒退秒數；超過時會完整重轉該段，避免段內發言順序錯置。 |
| `SPEAKER_BOUNDARY_ANCHOR` | `1` | 將相鄰分段的重疊音訊與上一段交界的匿名標籤對齊，避免每段重新從發言者 A 編號；不會傳送先前逐字稿內容。 |
| `SPEAKER_BOUNDARY_ANCHOR_MAX_AGE_SECONDS` | `45` | 尋找交界前最近可用發言者標籤的最大秒數；找不到時不強行指定。 |
| `INITIAL_DENSE_AUDIO_SPLIT` | `1` | 新上傳的長音檔若有效語音非常密集，首次轉錄即改用較短切段；指定分段重跑及僅重整摘要不套用，避免既有分段編號錯位。 |
| `INITIAL_DENSE_AUDIO_SPLIT_MINUTES` | `5` | 高語音密度來源首次轉錄的目標切段分鐘數。 |
| `INITIAL_VERY_DENSE_AUDIO_SPLIT_MINUTES` | `3` | 只有有效語音比例達極高門檻的區塊，首次轉錄會再縮短至此目標；可降低長時間連續討論時提早停寫的風險。 |
| `INITIAL_CLIPPED_DENSE_AUDIO_SPLIT` | `1` | 高語音密度且接近滿刻度峰值的來源，首次轉錄即採較短分段；不影響一般錄音或指定分段重跑。 |
| `INITIAL_CLIPPED_DENSE_AUDIO_SPLIT_MINUTES` | `3` | 爆音高密度來源首次轉錄的目標切段分鐘數。 |
| `INITIAL_CLIPPED_DENSE_AUDIO_CLIP_DBFS` | `-0.1` | 視為接近滿刻度實際爆音的峰值門檻；必須同時達到高語音密度才會觸發較短切段。峰值正常的高密度錄音維持較平衡的切段，減少不必要的交界。 |
| `INITIAL_DENSE_AUDIO_MIN_ACTIVE_SECONDS` | `180` | 觸發首次較短切段前，來源至少要有的有效語音秒數。 |
| `INITIAL_DENSE_AUDIO_MIN_ACTIVE_RATIO` | `0.55` | 觸發首次較短切段前，有效語音佔整段來源的最小比例。 |
| `INITIAL_VERY_DENSE_AUDIO_MIN_ACTIVE_RATIO` | `0.65` | 觸發極高密度短切段前，有效語音佔區塊的最小比例；不可低於一般高密度門檻。 |
| `INITIAL_DENSE_AUDIO_PER_SEGMENT_SPLIT` | `1` | 整場未達密集門檻時，仍逐一檢查各標準分段；只縮短連續發言的區塊，避免混合型會議的少數高密度討論段首次漏轉。 |
| `INITIAL_DENSE_AUDIO_SEGMENT_OVERLAP_SECONDS` | `5` | 僅對首次轉錄已因高密度而縮短的來源或局部討論段，額外保留交界語音上下文（0-10 秒）；一般錄音仍使用 `SEGMENT_OVERLAP_SECONDS`，降低連續句子切點的漏字風險。 |
| `RECOVERY_SUBSEGMENT_OVERLAP_SECONDS` | `2` | 補救小段在交界前後保留的上下文秒數；合併時僅移除完全相同的重疊發言。 |
| `RECOVERY_SHORT_SUBSEGMENT_MAX_SECONDS` | `30` | 小於或等於此秒數的補救小段，改用較長的交界語境；只作用於極端缺字等短小段重跑。 |
| `RECOVERY_SHORT_SUBSEGMENT_OVERLAP_SECONDS` | `4` | 短補救小段在交界前後保留的語音上下文秒數，降低連續發言剛好落在切點時的漏字風險。 |
| `SEGMENT_OVERLAP_DEDUPLICATION_WINDOW_SECONDS` | `15` | 輸出組裝時，在切點前後檢查下一段開頭是否與前段重疊；只移除有時間戳且可安全判定為重複的內容。 |
| `SEGMENT_OVERLAP_LEADING_FILLER_DEDUPLICATION` | `1` | 同一發言者若交界內容只差「好／那／對」等開頭口語詞，仍可安全去重；不同發言者或有新增內容的延續句會保留。 |
| `TRANSCRIPT_SPEECH_GAP_VALIDATION` | `1` | 啟用本機語音活動比對；只有長時間未標時間戳的區間仍有說話聲時，才觸發小段補救。 |
| `TRANSCRIPT_SPEECH_GAP_SECONDS` | `60` | 兩個時間戳相隔超過此秒數時，才進行本機語音活動確認；保留 15 秒容差，以符合逐字稿每 20-45 秒應標示時間戳的規則。 |
| `TRANSCRIPT_SPEECH_GAP_MIN_ACTIVE_SECONDS` | `12` | 缺口內至少需有多少秒非靜音音訊，才視為可能漏字。 |
| `TRANSCRIPT_SPEECH_GAP_MIN_ACTIVE_RATIO` | `0.25` | 缺口內非靜音音訊比例門檻；避免把真正的會議靜默誤判成漏字。 |
| `TRANSCRIPT_SPEECH_DENSITY_VALIDATION` | `1` | 啟用音訊與逐字稿文字量交叉檢查；僅在高語音活動段落的文字量異常偏低時觸發重跑。 |
| `TRANSCRIPT_SPEECH_DENSITY_MIN_ACTIVE_SECONDS` | `90` | 至少需有此秒數的有效語音，才進行文字密度判定。 |
| `TRANSCRIPT_SPEECH_DENSITY_SHORT_SEGMENT_MIN_ACTIVE_SECONDS` | `15` | 補救用短分段依長度下修文字密度的有效語音門檻時，仍保留的最小秒數；系統同時以分段長度上限約束門檻，避免 30 秒小段因不可能達到 45 秒語音而漏檢。 |
| `TRANSCRIPT_SPEECH_DENSITY_MIN_ACTIVE_RATIO` | `0.45` | 有效語音需佔分段的最小比例，避免背景雜訊造成誤判。 |
| `TRANSCRIPT_SPEECH_DENSITY_MIN_CHARS_PER_ACTIVE_SECOND` | `2.5` | 低於此每秒有效語音文字量時，視為逐字稿可能被過度省略。 |
| `TRANSCRIPT_LOCAL_DENSITY_VALIDATION` | `1` | 啟用滑動視窗檢查，找出整段總字數正常、但局部持續有聲卻被漏掉的內容。 |
| `TRANSCRIPT_LOCAL_DENSITY_WINDOW_SECONDS` | `90` | 局部文字密度的檢查視窗秒數。 |
| `TRANSCRIPT_LOCAL_DENSITY_STEP_SECONDS` | `45` | 相鄰檢查視窗的起點間距；較小可更精準定位，但會增加本機運算。 |
| `TRANSCRIPT_LOCAL_DENSITY_MIN_ACTIVE_SECONDS` | `35` | 視窗內至少需有此秒數有效語音，才進行局部文字量比對。 |
| `TRANSCRIPT_LOCAL_DENSITY_MIN_ACTIVE_RATIO` | `0.45` | 視窗內有效語音的最低比例，避免把正常靜默誤判為漏字。 |
| `TRANSCRIPT_LOCAL_DENSITY_MIN_CHARS_PER_ACTIVE_SECOND` | `1.5` | 低於此每秒有效語音文字量時，標出精確問題時間並交由局部補救。 |
| `TRANSCRIPT_LOCAL_DENSITY_MAX_RANGES` | `4` | 單一分段最多保留多少個局部漏字區間；過多時改採穩定小段重跑。 |
| `TRANSCRIPT_SPEECH_GAP_MAX_RANGES` | `6` | 每個分段最多保留幾個已由本機音訊活動確認的漏字位置；超過局部補救上限時改用穩定小段重跑。 |
| `TRANSCRIPT_REPAIR_CONTEXT_SECONDS` | `6` | 局部補救時額外提供缺口前後語境，改善句子與發言者承接；系統只合併缺口附近的時間戳內容。 |
| `TRANSCRIPT_REPAIR_COALESCE_GAP_SECONDS` | `20` | 已確認的漏字窗口若相隔不超過此秒數，視為同一個討論承接來合併補救，保留其他已驗證的逐字稿。設為 `0` 可關閉。 |
| `TRANSCRIPT_REPAIR_DIRECT_SPLIT_SECONDS` | `180` | 局部補救音檔達此秒數時，略過整段嘗試並直接切成小段轉錄，降低長缺口再次截斷或重複轉錄的機率。 |
| `TRANSCRIPT_CONFIRMED_GAP_RECOVERY_SECONDS` | `180` | 已確認有語音卻漏字或有多個可定位異常時，穩定重跑使用的目標切段秒數。 |
| `TRANSCRIPT_MULTI_GAP_RECOVERY_SECONDS` | `120` | 多個已確認的時間缺口超過局部補救上限，或指定重跑時音訊已證實逐字稿文字量偏低時，直接使用的更短重跑切段秒數；若已精確定位局部漏字、重複或數列異常，系統會優先採用 60 秒局部補救粒度。 |
| `TRANSCRIPT_LOCAL_REPAIR_RECOVERY_SECONDS` | `60` | 已定位的局部漏字、重複或數列異常，在局部補救時優先採用的較短切段秒數；可降低再次省略或按規律續寫的機率。 |
| `TRANSCRIPT_SEVERE_LOCAL_DENSITY_MAX_CHARS_PER_ACTIVE_SECOND` | `0.5` | 僅在本機已確認局部有效語音充足時，判定「文字量極端偏低」的文字密度上限（字/有效語音秒）。 |
| `TRANSCRIPT_SEVERE_LOCAL_DENSITY_RECOVERY_SECONDS` | `30` | 觸發極端局部缺字時的重跑小段秒數；只作用於已被音訊驗證的問題段，正常轉錄與一般局部漏字不受影響。 |
| `TRANSCRIPT_CRITICAL_RERUN_ESCALATION` | `1` | 首次轉錄或使用者選擇重跑時，若品質檢核或本機音訊已證實該段有極端缺字、多個持續語音缺口、數列延伸或長重複迴圈，系統會略過局部拼接，直接改用較短小段完整轉錄；指定分段重跑與整份重跑都會改用完整重跑模型。一般單點缺口與短暫重複仍沿用局部補救。 |
| `TRANSCRIPT_CRITICAL_SUSTAINED_GAP_SECONDS` | `60` | 本機已確認缺口內持續有語音時，單一缺口達此秒數便直接完整替換該分段，避免把近一分鐘漏轉的舊文字拼接回新稿；低於門檻的單點缺口仍優先局部補救。 |
| `TRANSCRIPT_CRITICAL_REPETITION_MIN_TURNS` | `8` | 同一句或同型句連續重複達此數量時，使用者選擇重跑會完整替換整段；低於門檻者保留已驗證文字，僅補救異常時間範圍。最小值為 `5`。 |
| `TRANSCRIPT_FRAGMENTATION_VALIDATION` | `1` | 偵測同一發言者長時間輸出大量短而懸空的片段。命中時會以 60 秒小段交由獨立轉錄模型補救一次；若仍無改善則保留精確警示，不會無限重跑或阻斷交付。 |
| `TRANSCRIPT_FRAGMENTATION_MAX_TURN_CHARS` | `12` | 納入語句碎裂偵測的最長發言字數。 |
| `TRANSCRIPT_FRAGMENTATION_MIN_SHORT_TURNS` | `10` | 同一檢視窗內至少需要多少短發言才會標示。 |
| `TRANSCRIPT_FRAGMENTATION_MIN_DOMINANT_SPEAKER_RATIO` | `0.80` | 短發言需由同一位發言者占的最低比例，可排除多人快速問答。 |
| `TRANSCRIPT_FRAGMENTATION_MIN_DANGLING_SHORT_RATIO` | `0.40` | 短發言中以「都、因為、是」等疑似未完成語句結尾的最低比例，可排除內容完整的短答。 |
| `TRANSCRIPT_FRAGMENTATION_WINDOW_SECONDS` | `120` | 進行語句碎裂偵測的時間視窗；另需跨越至少 `60` 秒。 |
| `TRANSCRIPT_SHORT_CYCLE_DUPLICATE_VALIDATION` | `1` | 偵測短時間內的近似長句重複；命中時僅補救可定位時間範圍。 |
| `TRANSCRIPT_SHORT_CYCLE_DUPLICATE_MIN_CHARS` | `20` | 納入短週期近似重複比對的最短發言長度。 |
| `TRANSCRIPT_SHORT_CYCLE_DUPLICATE_MIN_SIMILARITY` | `0.88` | 兩句文字需達到的相似度門檻。 |
| `TRANSCRIPT_SHORT_CYCLE_DUPLICATE_MIN_TURNS` | `3` | 至少幾句高度相似才視為異常。 |
| `TRANSCRIPT_SHORT_CYCLE_DUPLICATE_WINDOW_TURNS` | `4` | 搜尋異常時採用的相鄰發言視窗大小。 |
| `TRANSCRIPT_SHORT_CYCLE_DUPLICATE_MAX_SPAN_SECONDS` | `30` | 同一近似重複視窗允許的最大時間跨度。 |
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

`GET /health` 會回傳載入中的 Git commit、工作區 commit、程式碼指紋、`matches_workspace`、worker 狀態、資料庫 schema version 與分段快取版本。若修改程式後尚未重啟，`matches_workspace` 會變成 `false` 且狀態為 `degraded`；這只能表示服務載入版本過舊，不代表要在仍有執行中任務時直接重啟。

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

- 每筆會議都有獨立人工審查狀態：`AI 產出`、`待人工複核`、`已人工複核`、`已核准`。核准時會保存當下 Markdown SHA-256；摘要、逐字稿或補充佐證有任何變更，都會自動撤銷核准並退回待複核。仍有阻擋交付問題的分段不能核准。
- 新產生的討論 D、決議 R、待辦 A 除了寫入 Markdown，也會保存為結構化 JSON 與逐項索引。舊會議維持原內容，不會因 schema 升級而被重寫。
- 每個逐字稿分段都有「重跑本段」與「完整重跑」按鈕。「重跑本段」若品質檢查已定位到原始媒體仍有聲音的時間缺口，或可由連續時間戳安全界定的重複轉錄/數列延伸異常，會先只轉錄該異常區間並安全合併回既有逐字稿；局部補救無法通過檢查時才改用整段穩定重跑。「完整重跑」則明確略過舊稿與局部補救，以約 60 秒小段、設定的完整重跑模型重新轉錄指定段，適合時間戳完整但文字內容已失真的情況；它只做一輪轉錄，不會額外重複呼叫模型。按整份「重跑」也會從原始媒體重新轉錄所有新切段而不使用快取；若舊品質報告已有重大問題，會按問題的實際時間範圍對應新切段後自動改用完整重跑模型。舊紀錄缺少有效起訖時間時，系統會保守地將整份改用完整重跑策略，不會猜測舊索引。「完整逐字稿品質檢核」同時會套用不需模型的術語正規化；只會寫入確實有差異的紀錄，並保留「術語正規化前版本」以便復原。
- 無論是單段短音檔或多段長音檔，若主模型與第二模型都尚未完全通過音訊品質檢查，系統只會保存可驗證問題較少的「補救候選稿」到受原始檔 SHA-256、分段範圍、模型與詞彙表綁定的續跑資料；它不會產生摘要或覆蓋既有會議。下次同一段重跑時，系統會以該候選稿為底，只補救剩餘問題範圍。
- 轉錄遇到無法確認、且無法組成合理句子的字詞時，會保守標記為 `[聽不清]`，不會依前後文硬補。品質報告會列出含此標記的分段；摘要與第二階段查核不會把這些未確認內容推論為決議、負責人、期限或待辦事項。
- 「語意檢核」是手動背景任務，使用文字模型只找出高度明確的語句失真，並把分段與時間位置標為需回聽。它不讀取或改寫音檔、不改寫逐字稿與摘要、不自動重跑；判定結果僅供選擇「完整重跑」前複核。主模型失敗時才會使用一次備援模型。
- 「重新檢核」只用既有逐字稿與原始媒體檔重新計算問題分段，不呼叫 Gemini、不產生新會議。已成功補救的舊歷程會保留為註記，只有目前仍有音訊佐證的缺口或仍存在的轉錄異常會列為可重跑問題。
- 搜尋列的「全部檢核」會以背景任務逐份更新所有會議的品質報告；同樣不呼叫 Gemini、不產生新會議。完成後可用「只看需複核」直接查看已定位的問題分段並指定重跑。
- 「重整摘要」沿用完整逐字稿，只重新產生摘要、決議與待辦，不會再次轉錄音訊。
- 「高品質重整」會在一般摘要後增加一次第二模型證據查核，因此會多使用一次模型請求；只有手動點選時才會啟用。
- 詳情頁會直接載入保留的原始錄音或錄影；逐字稿中的時間戳可跳回對應媒體時間，也可用「開啟」在新分頁播放或用「下載」保存原始檔。
- 「摘要」只修改摘要、決議與待辦；「逐字稿」只修改完整逐字稿。兩種修改都會在儲存前把完整舊版保存在「版本」中，第一次修改保留的版本即為 AI 原稿。
- 修正逐字稿後，可再按「重整摘要」或「高品質重整」，系統會沿用修正版逐字稿重新產出討論摘要、最終決議與待辦事項。

### 補充資料與截圖佐證

在 Web 歷史記錄打開任一會議後，可點選「補充資料」上傳會議相關檔案；系統會將檔案保存到 `output/attachments/meeting_<會議ID>/`，再請 Gemini 檢視內容、判斷與該會議的關聯性，最後把分析結果追加到同一份 Markdown 的「📎 五、補充資料與佐證」區塊。成功後會同步保存附件 SHA-256、分析 metadata、舊版 Markdown revision 並更新全文搜尋；分析或入庫失敗時不保留孤立附件。

目前支援 `.png`、`.jpg`、`.jpeg`、`.webp`、`.pdf`、`.txt`、`.md`、`.csv`、`.docx`。圖片與 PDF 會直接交由 Gemini 視覺/文件能力判讀；文字、Markdown、CSV 與 Word 會先抽取文字再分析。

AI 會輸出「系統判斷」、「擷取重點」、「對會議記錄的影響」、「可能矛盾或待確認」與「來源註記」，並要求明確區分「逐字稿提到」、「補充資料顯示」、「系統推論」、「需人工確認」。系統不會自動改寫原摘要、決議或待辦事項，而是以佐證區塊保留 AI 建議，方便人工確認後再採用；追加佐證也會讓已核准記錄退回待複核。

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
