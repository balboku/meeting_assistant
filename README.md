# 🎙️ AI 語音會議助理 (AI Voice Meeting Assistant)

> 讀取本地音訊/影片或透過 **Web／桌面 GUI** 傳送語音，利用 **Google Gemini API** 原生音訊處理能力，自動生成完整逐字稿與結構化會議記錄。

---

## 🗂️ 完整專案結構

```
meeting_assistant/
├── meeting_assistant.py    # Phase 0：CLI 快速處理腳本
├── backend/                # Phase 1：FastAPI 後端（核心 API）
│   ├── main.py             #   FastAPI 入口與路由
│   ├── database.py         #   SQLite 資料庫（歷史記錄、支援刪除）
│   ├── tasks.py            #   Gemini AI 背景任務（含長音訊/影片自動切割處理）
│   ├── previous_minutes.py #   前次 Word 安全解析、追蹤規則與來源雜湊
│   ├── evidence.py         #   補充資料 / 截圖判讀並追加到會議記錄
│   └── models.py           #   Pydantic 資料結構
├── gui/                    # Phase 2：桌面錄音 GUI
│   ├── app.py              #   Tkinter 主視窗（執行此檔案）
│   ├── recorder.py         #   sounddevice 錄音封裝
│   └── api_client.py       #   後端 HTTP 通訊客戶端
├── static/                 # Phase 4：網頁版前端介面
│   └── index.html          #   提供網頁上傳、歷史瀏覽、原始媒體核對、品質修訂與刪除功能
├── output/                 # AI 生成的 Markdown、原始媒體檔與補充資料附件（自動建立）
│   ├── source_audio/       # 已上傳的原始錄音/錄影保留區（沿用舊資料夾名稱）
│   └── previous_minutes/   # 操作者上傳的前次會議紀錄 .docx 保留區
├── temp/                   # 分段與處理中暫存檔（自動建立）
├── requirements.txt        # 套件相依清單
├── .env                    # 您的私密 API Key（不要上傳 Git！）
└── .env.example            # 環境變數範本
```

---

## 📦 環境建置

### 步驟 1：確認 Python 版本

```bash
python3.14 --version  # 建議 Python 3.14
```

### 步驟 2：安裝相依套件

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install --upgrade pip setuptools wheel
.venv/bin/python -m pip install -r requirements.txt
```

若要完全重現目前驗證過的 Python 3.14 環境，請使用 `requirements.lock`。

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
APP_API_KEY=change_me_to_a_long_random_value
MEETING_AUTH_ENABLED=0
MEETING_AUTH_USER_HEADER=X-Meeting-User
MEETING_AUTH_DEFAULT_ROLE=viewer
MEETING_AUTH_TRUSTED_PROXY_NETWORKS=127.0.0.0/8,::1/128
MEETING_AUTH_LAN_SESSION_USER=meeting-lan-editor@meeting-assistant.local
MEETING_AUTH_TRUSTED_LOCAL_NETWORKS=
MAX_UPLOAD_MB=500
PREVIOUS_MINUTES_MAX_MB=20
PREVIOUS_MINUTES_MAX_TEXT_CHARS=50000
CORS_ALLOWED_ORIGINS=http://127.0.0.1:8001,http://localhost:8001
MEETING_ASSISTANT_TRUST_LOCAL_NETWORK=0
MEETING_ASSISTANT_SHARE_HOST=
DB_PATH=./meetings.db
MEETING_TEMP_DIR=./temp
MEETING_OUTPUT_DIR=./output
MEETING_SOURCE_AUDIO_DIR=./output/source_audio
MEETING_PREVIOUS_MINUTES_DIR=./output/previous_minutes
MEETING_ATTACHMENT_DIR=./output/attachments
MEETING_BACKUP_DIR=./backups
MEETING_OFFSITE_BACKUP_DIR=
BACKUP_MIN_INTERVAL_HOURS=168
MEETING_DOCX_TEMPLATE_PATH=./4-QA-005 V01 會議紀錄.docx
DB_BACKUP_KEEP=4
JOB_RETENTION_DAYS=30
JOB_QUEUE_MAX_ATTEMPTS=5
JOB_QUEUE_QUALITY_MAX_ATTEMPTS=3
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

安全預設：loopback 可由本機使用，區網入口必須先取得有效的 bootstrap/API session，不把「同 Wi-Fi」當成身分。若明確啟用長效區網網址，必須同時指定精確 CIDR 與獨立的 LAN 使用者；不能將所有私有網段或 LAN 同仁映射成本機管理員。

帳號、角色、稽核紀錄與中央路由權限政策已完成。`MEETING_AUTH_ENABLED=1` 時，本機 loopback 或有效 API session 會映射到 `MEETING_AUTH_LOCAL_SESSION_USER`；明確信任 CIDR 內的直接連線則映射到 `MEETING_AUTH_LAN_SESSION_USER`。只有 `MEETING_AUTH_TRUSTED_PROXY_NETWORKS` 內的代理可以提供身分 header，外部用戶端直接偽造會被拒絕。所有 jobs、meetings、source-media 與 admin 路由都依最小權限檢查，角色只從 `app_users` 讀取。

> 資安提醒：不要提交 `.env`、`meetings.db*`、`temp/`、`output/`、`backups/`、`logs/`、原始錄音、會議記錄或匯出的文件。若金鑰曾暴露，請立即輪換 `GEMINI_API_KEY` 與 `APP_API_KEY`。

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
| `MEETING_PREVIOUS_MINUTES_DIR` | `./output/previous_minutes` | 選填前次會議紀錄 `.docx` 的保留資料夾；佇列重試與摘要重整會沿用相同來源。 |
| `PREVIOUS_MINUTES_MAX_MB` | `20` | 前次會議紀錄 Word 的壓縮檔大小上限。 |
| `PREVIOUS_MINUTES_MAX_TEXT_CHARS` | `50000` | 從前次 Word 擷取並提供摘要模型的文字上限；超出內容會標示為已截斷。 |
| `MEETING_ATTACHMENT_DIR` | `./output/attachments` | 會議補充資料、截圖、PDF、文件的保存位置。 |
| `MEETING_BACKUP_DIR` | `./backups` | 啟動維護時保存一致性 SQLite 備份與 v2 完整記錄快照的位置。 |
| `MEETING_OFFSITE_BACKUP_DIR` | 空白 | 可選的不同磁碟或網路分享路徑；設定後會原子複製 v2 快照、再驗證 SHA-256/ZIP/SQLite。不可與本機備份目錄相同。 |
| `MEETING_DATABASE_MAX_BYTES` | `2147483648` | `/health` 的 SQLite 容量上限；超過時降級，避免無聲逼近單機容量。 |
| `MEETING_SOURCE_MEDIA_MAX_BYTES` | `21474836480` | 原始媒體容量健康門檻。 |
| `MEETING_BACKUP_MAX_BYTES` | `21474836480` | 本機備份容量健康門檻。 |
| `MEETING_MIN_FREE_DISK_BYTES` | `5368709120` | 本機資料磁碟的最低可用空間。 |
| `BACKUP_MIN_INTERVAL_HOURS` | `168` | 啟動維護只有在資料／媒體狀態已變更且距前份快照至少 7 天時，才新增一致性 SQLite 備份與 v2 快照；損壞或缺少備份會立即修復。 |
| `FULL_SNAPSHOT_MIN_INTERVAL_HOURS` | 未設定 | 舊版相容別名；只有未設定 `BACKUP_MIN_INTERVAL_HOURS` 時才採用，建議改用新名稱。 |
| `MEETING_DOCX_TEMPLATE_PATH` | `./4-QA-005 V01 會議紀錄.docx` | Word 匯出使用的本機範本路徑。公司表單範本請保留在本機，不提交到 Git。 |
| `DB_BACKUP_KEEP` | `4` | 本機 SQLite、完整記錄快照與異地快照各保留最近 4 份。 |
| `SOURCE_MEDIA_ARCHIVE_RETENTION_DAYS` | `90` | 手動移除原始錄音/錄影後，`backups/source_media_deleted/` 備份保留天數；設為 `0` 可停用自動清理。 |
| `JOB_RETENTION_DAYS` | `30` | 已完成、失敗或取消任務的保留天數。 |
| `JOB_QUEUE_MAX_ATTEMPTS` | `5` | 自動處理任務最多嘗試次數；用於降低 503/暫時性服務忙碌造成的失敗。 |
| `JOB_QUEUE_QUALITY_MAX_ATTEMPTS` | `3` | 非暫時性的轉錄品質失敗最多嘗試次數；避免對穩定缺陷重複消耗模型額度。 |
| `JOB_QUEUE_LEASE_SECONDS` | `90` | 全域 worker 與處理中任務 lease；多 Uvicorn 行程只能有一個有效 worker。 |
| `JOB_QUEUE_HEARTBEAT_SECONDS` | `15` | worker/任務 lease 續約頻率；失去 fencing generation 的行程不可提交結果。 |
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
| `MEETING_ASSISTANT_TRUST_LOCAL_NETWORK` | `0` | 是否允許 `MEETING_AUTH_TRUSTED_LOCAL_NETWORKS` 內的直接來源使用 LAN 身分開啟 Web；安全預設關閉。 |
| `MEETING_ASSISTANT_SHARE_HOST` | 空白 | 一鍵啟動時顯示的固定 DNS／NetBIOS 主機名；空白時使用目前 DHCP IP。 |
| `MEETING_AUTH_ENABLED` | `0` | 帳號/角色權限開關。啟用前須先建立 `MEETING_AUTH_LOCAL_SESSION_USER`；啟用後中央政策會保護所有業務路由。 |
| `MEETING_AUTH_LOCAL_SESSION_USER` | `local-admin@meeting-assistant.local` | loopback 或有效 API session 對應的持久化帳號；必須先存在於 `app_users`。 |
| `MEETING_AUTH_LAN_SESSION_USER` | `meeting-lan-editor@meeting-assistant.local` | 長效區網網址使用的獨立持久化帳號；建議只給 `editor` 或 `viewer`，不要給 `admin`。 |
| `MEETING_AUTH_TRUSTED_LOCAL_NETWORKS` | 空白 | 允許長效區網存取的精確 CIDR 清單，例如 `192.168.20.0/24`；空白時即使開啟 trust 也不放行。 |
| `MEETING_AUTH_USER_HEADER` | `X-Meeting-User` | 啟用帳號權限時由可信任代理提供的使用者身分 header；角色必須先寫在 `app_users`。 |
| `MEETING_AUTH_DEFAULT_ROLE` | `viewer` | 啟用帳號權限時，既有使用者資料缺少角色時的保守預設。 |
| `MEETING_AUTH_TRUSTED_PROXY_NETWORKS` | `127.0.0.0/8,::1/128` | 允許提供身分 header 的反向代理網段；不可直接設成所有網路。 |
| `MEETING_ASSISTANT_AUTO_RESTART` | `1` | Uvicorn 異常退出時由 `start.py` 監督重啟。新的 supervisor token 會阻止舊啟動器搶回服務。 |
| `MEETING_ASSISTANT_MAX_RESTARTS` | `5` | 連續異常退出的重啟上限。 |
| `MEETING_ASSISTANT_RESTART_DELAY_SECONDS` | `2` | 指數退避的起始秒數。 |
| `MEETING_ASSISTANT_RESTART_RESET_SECONDS` | `300` | Uvicorn 穩定運行達此秒數後重設連續失敗計數。 |

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

網頁介面的「維運狀態」列會顯示原始媒體容量與「待確認欄位」；後者會開啟 `meeting_confirmation_tasks` 佇列，可逐項補回負責人、期限或時間碼，或明確略過。未連結媒體的移除會先搬到 `backups/source_media_deleted/` 並保存 metadata，不會直接永久刪除；啟動維護再依 `SOURCE_MEDIA_ARCHIVE_RETENTION_DAYS` 清理過期封存。

在「上傳音訊/影片」視窗可選擇一份前次會議紀錄 Word（`.docx`）。系統保留原始檔名與 SHA-256，將前次決議／待辦／風險整理到「前次會議追蹤」；本次完成、延期、取消等狀態仍必須有本次逐字稿時間碼。若本次沒有提到，輸出會標示「本次未討論」，不會把前次決議誤列為本次決議。舊 `.doc`、加密檔、含巨集內容、異常壓縮封裝、純掃描圖片或無可讀文字的 Word 會被拒絕。

若會議紀錄已經產生，可在詳情頁按「📄 補前次重產」再上傳一份 `.docx`。系統會保留原紀錄、沿用其完整逐字稿、預設增加第二模型證據查核，並建立一筆新的會議紀錄；新紀錄保存來源會議 ID、來源 job ID、原逐字稿 SHA-256 與前次 Word SHA-256，方便追溯。若原紀錄缺少完整逐字稿或可用的原始媒體，系統會拒絕重產，不會建立脈絡不完整的新紀錄。

### 手機 / 平板開啟 Web 介面

一鍵啟動時，終端機會列出「手機 / 平板」網址，例如：

```text
手機 / 平板：http://192.168.1.20:8001/history?bootstrap_token=...
```

請讓手機與執行後端的 Mac / PC 連到同一個 Wi-Fi，再開啟終端機列出的短效 bootstrap 網址；驗證成功後會換成不含金鑰的 session cookie。區網預設不匿名放行。

若已明確設定固定分享主機、精確區網 CIDR 與 LAN 使用者，終端機會改列出不含 token 的長效網址，例如 `http://NB-RD-BALBO:8001/history`。它只接受該 CIDR 的直接連線，LAN 使用者不會取得本機管理員權限。HTTP 區網網址可瀏覽、上傳與管理任務；瀏覽器的麥克風、鏡頭及螢幕錄製通常要求 HTTPS 安全來源，若同仁需要直接在自己的瀏覽器錄音／錄影，應改部署內部 HTTPS。

Windows 首次啟用時，請以「系統管理員」PowerShell 執行下列腳本；即使公司政策將 Wi-Fi 分類為公用網路，規則仍只放行指定 CIDR，不會允許其他公用網路來源：

```powershell
.\scripts\enable_lan_access.ps1 -TrustedSubnet 192.168.20.0/24
```

若要撤回區網入口：

```powershell
.\scripts\enable_lan_access.ps1 -Disable
```

如果手機仍無法開啟，請先確認：
- 手機與 Mac / PC 在同一個 Wi-Fi，且不是訪客網路或 AP isolation 網路。
- Mac / Windows 防火牆允許 Python / uvicorn 接受區域網路連線。
- 一鍵啟動終端機仍在執行，且沒有顯示 port 被其他程式佔用。

### B. 手動啟動 FastAPI 後端與網頁介面（Phase 1 & 4）

```bash
# 後端 API Server 與靜態網頁（Port 8001，避免衝突）
.venv/bin/python -m uvicorn backend.main:app --host 0.0.0.0 --port 8001

# 🌐 開啟網頁版介面 (Phase 4)
open http://127.0.0.1:8001/history

# 🛠️ Swagger UI 測試介面
open http://127.0.0.1:8001/docs
```

`GET /livez` 只確認 API 程序存活；`GET /readyz` 驗證 schema 與全域 worker lease，未就緒時回 503。`GET /health` 回傳載入中的 Git commit、工作區 commit、程式碼指紋、`matches_workspace`、worker、schema、SQLite quick check、媒體工具、本機／異地備份與容量門檻。大型快照的完整驗證依路徑、大小與修改時間快取，檔案變更即失效。超過 8 天的備份若 durable record-state 與媒體 inventory 仍和現況相同，會標示 `state_current=true` 並維持健康；快照可讀但 manifest 有缺檔時仍會回報 `container_valid=true`、`recoverability_complete=false` 並降級。

啟動維護每次都會驗證現有備份；只有資料或媒體狀態已變更且距前份快照至少 7 天，才新增 `meetings_*.db` 與 `meeting_records_*.zip`，各保留 4 份。損壞／缺少備份會立即補建，未變更的已驗證備份可長期沿用；異地路徑缺檔時會從本機快照補同步。v2 ZIP 包含一致性資料庫、Markdown、補充附件及已連結原始錄音／錄影；媒體以 SHA-256 內容定址去重，manifest 保存逐檔雜湊。

高風險操作前或需要立即建立備份時，先確認沒有 pending／processing 任務，再執行手動強制備份；此命令不受 7 天週期限制：

```powershell
.\.venv\Scripts\python.exe scripts\run_backup.py
```

還原工具只接受空目錄，並另建 `runtime/meetings.db`、`runtime/output/`、`runtime/evidence/`，重寫資料庫路徑後執行 integrity/FK 檢查：

```bash
.venv/bin/python scripts/verify_record_snapshot.py backups/meeting_records_YYYYMMDD_HHMMSS.zip
.venv/bin/python scripts/restore_record_snapshot.py backups/meeting_records_YYYYMMDD_HHMMSS.zip restore-drill
```

部署依賴固定於 `requirements.lock` 並含套件雜湊；`.python-version`、Windows/Linux CI 均使用 Python 3.14。CI 另執行 `pip-audit` 與 `scripts/check_architecture.py`，阻擋已知相依弱點及既有大型模組繼續成長。

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

## 📄 輸出格式

生成的 Markdown 檔案包含以下四個區塊：

| 區塊 | 說明 |
|------|------|
| 🔁 **前次會議追蹤** | 上傳前次 Word 時才出現；列出前次事項、本次狀態、本次更新與本次逐字稿佐證 |
| 📋 **會議摘要** | 300 字以內重點概述 |
| ✅ **重要決議** | 明確達成的決議（條列式） |
| 📌 **待辦事項** | 任務 / 負責人 / 期限（表格） |
| 📝 **完整逐字稿** | 區分講者 + 時間戳記 |
| 📎 **補充資料與佐證** | 使用者追加截圖 / 文件後，由 AI 判讀關聯性並補入；此區塊只有在上傳補充資料後出現 |

長音訊會先切成 10 分鐘分段轉錄，再合併為完整逐字稿。合併時會把分段內的 `[00:00]`、`[09:59]` 等相對時間戳轉成全會議時間，例如第二段會顯示為 `[10:00]`、`[19:59]`。

### Web 品質修訂工具

- 每筆會議都有獨立人工審查狀態：`AI 產出`、`待人工複核`、`已人工複核`、`已核准`。核准時會保存當下 Markdown SHA-256；摘要、逐字稿或補充佐證有任何變更，都會自動撤銷核准並退回待複核。仍有阻擋交付問題的分段不能核准。
- 新產生的討論 D、決議 R、待辦 A 除了寫入 Markdown，也會保存為結構化 JSON 與逐項索引；可逐項複核／核准，補充佐證可關聯到指定 D/R/A 代碼。舊會議維持原內容，不會因 schema 升級而被重寫。
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

### Q：媒體檔上傳逾時
確認網路穩定，或在 `backend/tasks.py` 調大 `MAX_UPLOAD_WAIT_SECONDS`

---

*Powered by Google Gemini API | AI 語音會議助理*
