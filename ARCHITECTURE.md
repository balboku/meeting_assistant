# 🎙️ AI 語音會議助理 — 現行系統架構文件 v2.5

> **文件版本**：2.5.0
> **更新日期**：2026/07/30
> **現況**：FastAPI、SQLite 持久化佇列、Web/GUI、多段轉錄、品質閘門、人工複核與全文搜尋均為現行功能；LINE webhook 與 ngrok 自動 tunnel 已移除。

---

## 一、系統架構圖 (System Layers)

```mermaid
graph TB
    subgraph CLIENT["🖥️ 1. 使用者介面層 (Client)"]
        WEB["🌐 Web 歷史與複核介面"]
        GUI["💻 桌面錄音工具\n(Python GUI · sounddevice)"]
    end

    subgraph BACKEND["⚙️ 2. 核心後端層 (Backend)"]
        FASTAPI["🚪 API 網關\nFastAPI"]
        PREPROCESS["🔧 媒體預處理模組\nPydub · 格式轉換 / 切割"]
        QUEUE["📋 SQLite 持久化佇列\nfenced lease 單一有效 Worker"]
    end

    subgraph AI["🤖 3. AI 服務層 (AI Services)"]
        TRANSCRIBE["✨ Gemini File API\n逐段轉錄 / 補救 / 重跑"]
        SUMMARY["🧠 摘要模型\nJSON D/R/A + 可選第二模型查核"]
    end

    subgraph STORAGE["💾 4. 資料儲存層 (Storage)"]
        TEMP["📁 Temp 資料夾\n(分段 / 處理暫存 · 自動刪除)"]
        SOURCE["🎧 Source Media\n(原始錄音/錄影保留)"]
        OUTPUT["📂 Output 資料夾\n(Markdown 會議記錄)"]
        ATTACH["📎 Attachments\n補充佐證與 SHA-256"]
        SQLITE["🗄️ SQLite + FTS5\n佇列 / 結構資料 / 版本 / 搜尋"]
    end

    WEB --> FASTAPI
    GUI -- "HTTP POST /upload-media" --> FASTAPI
    FASTAPI --> PREPROCESS
    FASTAPI --> SOURCE
    PREPROCESS --> TEMP
    PREPROCESS --> QUEUE
    QUEUE --> TRANSCRIBE
    TRANSCRIBE -- "含時間戳逐字稿" --> QUEUE
    QUEUE --> SUMMARY
    SUMMARY -- "結構化 JSON D/R/A" --> QUEUE
    QUEUE --> OUTPUT
    QUEUE -- "metadata + structured items" --> SQLITE
    FASTAPI --> ATTACH
    ATTACH --> SQLITE
    QUEUE -- "結果通知" --> GUI
```

---

## 二、層級說明總覽

| 層級名稱 | 負責模組與技術堆疊 | 主要任務 |
| --- | --- | --- |
| **1. 使用者介面層 (Client)** | Web 歷史與複核介面<br>Python GUI (Tkinter/PyQt) + `sounddevice` | 收集音訊或影片來源（實體錄音 / 線上擷取 / 螢幕錄製），並向後端發送請求，最後展示結果給使用者。 |
| **2. 核心後端層 (Backend)** | Python + FastAPI<br>SQLite 持久化佇列 + 單一 Worker | 接收媒體檔、驗證、排程、重試、取消、處理品質閘門，並提供審查與維運 API。 |
| **3. AI 服務層 (AI Services)** | Gemini 轉錄模型 + 摘要/查核模型 | 先產生可追溯逐字稿，再以 JSON contract 產生 D/R/A；高品質模式增加第二模型證據查核。 |
| **4. 資料儲存層 (Storage)** | 本地檔案 + SQLite/FTS5 | 保存原始媒體、Markdown、附件、任務事件、結構化 D/R/A、人工審查狀態與修訂歷史。 |

---

## 三、各模組功能詳細拆解

### 1. 使用者介面層 (Client)

針對實體與線上會議，保留 Web 上傳與桌面錄音兩個受控入口。

#### 🌐 實體會議入口 (Web)
- **情境**：使用手機或錄音設備完成錄音後，從受控 Web 介面上傳。
- **功能**：支援音訊與影片檔案，上傳後可在同一介面追蹤任務與複核結果。
- **運作**：loopback 可直接使用；區網來源須透過短效 bootstrap/API session。

#### 💻 線上會議入口 (桌面錄音小工具)
- **情境**：在電腦前開 Google Meet、Teams 或 Zoom。
- **功能**：極簡視窗，只有「錄音 / 停止」按鈕。
- **運作**：透過 `sounddevice` 或 `pyaudio`，**同時擷取系統喇叭（別人說話）與麥克風（自己說話）**，混音存成本地 `.mp3` 後，自動打 API 送給後端。

---

### 2. 核心後端層 (Backend)

> 可跑在筆電上，或部署於免費雲端平台（如 Render / Fly.io）。

| 子模組 | 技術 | 功能 |
|--------|------|------|
| **API 網關** | FastAPI | 開出 `/upload-media`（Web／桌面端上傳；`/upload-audio` 保留相容）及審查、搜尋、匯出與維運端點 |
| **媒體預處理** | Pydub | 必要時抽取/轉換音訊並切割，保留原始音訊或影片作為證據檔 |
| **任務佇列** | SQLite `jobs` + 單一 Worker | 跨服務重啟保留任務，支援 retry/backoff、取消、進度與事件時間線。 |

---

### 3. AI 服務層 (AI Services)

> **架構亮點**：轉錄與摘要分離。逐字稿先通過時間戳、音訊覆蓋、重複幻覺與快取安全檢查，再交給摘要模型；因此可以單獨重跑問題段或只重整摘要。

| 方案 | 技術 | 優勢 | 限制 |
|------|------|------|------|
| ⭐ **現行方案（已實作）** | Gemini File API + 分段轉錄 + JSON 摘要 | 可分段補救、保留時間證據、獨立重整摘要 | 仍受上游服務可用性與模型品質影響 |
| 備選方案 | OpenAI Whisper → GPT-4o | 逐字稿品質高、語言支援廣 | 需兩次 API、成本較高、Whisper 25MB 限制 |

**Output Prompt 四大區塊**：
1. 📋 會議摘要 (Executive Summary) — 300 字以內
2. ✅ 重要決議 (Key Decisions) — 條列式
3. 📌 待辦事項 (Action Items) — 表格（任務、負責人、期限、優先級）
4. 📝 完整逐字稿 (Verbatim Transcript) — 附講者標記與時間戳記

---

### 4. 資料儲存層 (Storage)

保持輕量，按需擴展：

```
meeting_assistant/
├── temp/                          ← 分段與處理暫存檔（自動刪除）
├── output/
│   ├── source_audio/              ← 已上傳的原始錄音/錄影（處理後保留，沿用舊資料夾名稱）
│   ├── 2026-07-04_Marketing.md   ← 日期命名的會議記錄
│   ├── 2026-07-05_Sync.md
│   └── ...
└── meetings.db                    ← SQLite（可選，歷史搜尋用）
```

**SQLite 資料表設計（現行）**：

| 資料表 | 用途 |
|--------|------|
| `meetings` | 保存會議主檔、品質報告、結構化摘要、人工審查狀態與核准內容 SHA-256。 |
| `meeting_items` | 將 D/R/A 逐項保存成 JSON 索引，保留證據時間、逐項複核／核准狀態、複核者與備註。 |
| `meeting_evidence` | 保存補充附件檔名、路徑、SHA-256、分析內容與對應 revision。 |
| `meeting_evidence_items` | 將補充佐證關聯到一或多個 D/R/A 項目；關聯可與文件版本、附件雜湊一併稽核。 |
| `meeting_confirmation_tasks` | schema v6 的人工確認佇列；保存缺少的負責人、期限或時間碼及 resolved/waived 證據。 |
| `meeting_fts` | SQLite FTS5 虛擬表，索引 `title`、`source_audio`、`summary`、`output_path`，支援快速的欄位搜尋。 |
| `meeting_content_fts` | SQLite FTS5 虛擬表，索引每筆會議的完整 Markdown 內容，支援逐字稿搜尋。 |
| `meeting_revisions` | 保存人工修訂摘要或逐字稿前的完整舊版 Markdown，供回溯 AI 原稿與修改歷史。 |
| `jobs` | 持久化媒體處理佇列，保存狀態、payload、attempts、取消旗標與進度欄位。 |
| `job_events` | 任務事件時間線，記錄建立、worker claim、狀態轉換、retry、取消等事件，供維運與 UI 觀察流程。 |
| `job_event_archive` | schema v5 升級前缺少父任務的歷史事件封存；保留原事件而不讓孤兒資料破壞外鍵。 |
| `runtime_leases` | 保存全域 worker 與啟動維護 lease、heartbeat、generation fencing token。 |
| `app_users` | 帳號與角色表；`MEETING_AUTH_ENABLED=0` 時維持既有本機模式，啟用後中央路由政策全面執行最小權限。 |
| `audit_logs` | 保存 actor、action、resource 與 request metadata；文件與逐項審查變更會建立稽核紀錄。 |
| `app_meta` | 保存資料庫 schema version，供 `/health` 驗證執行版本。 |
| `schema_migrations` | 保存每版 migration 套用時間與資料修復明細；升級前先建立 SQLite online backup。 |

搜尋流程依序合併欄位 FTS、完整內容 FTS 與參數化 `LIKE` 後備搜尋；後備搜尋補足 SQLite `unicode61` 對中文連續字串部分匹配的限制。兩個 FTS 索引在新增、編輯、刪除會議時增量更新，啟動時只對缺漏的既有資料進行一次性補建，搜尋本身維持唯讀。若部署環境的 SQLite 不支援 FTS5，API 仍可使用 `LIKE` 搜尋欄位與完整內容。

Web 歷史頁可從 `/meetings/{id}/source-media` 串流保留的原始錄音或錄影，並把逐字稿時間戳連回播放器；舊的 `/meetings/{id}/source-audio` 仍保留相容。人工修訂分成兩條路徑：`PUT /meetings/{id}/summary` 只改摘要、決議與待辦；`PUT /meetings/{id}/transcript` 只改完整逐字稿。兩者都會先寫入 `meeting_revisions`，再更新 Markdown 與 FTS 索引，並將會議退回 `needs_review`。`PUT /meetings/{id}/review` 明確保存人工複核／核准；核准時記錄目前 Markdown SHA-256，有阻擋交付的品質問題時拒絕核准。

補充佐證經 `POST /meetings/{id}/evidence` 上傳後，Gemini 的同步 SDK 呼叫會放入工作執行緒，避免阻塞 FastAPI event loop。成功時附件、SHA-256、分析內容、D/R/A 關聯、revision 與全文索引一致更新；失敗時尚未入庫的附件會清理。`PUT /meetings/{id}/items/{item_key}/review` 提供逐項複核／核准，逐項狀態會回捲整份文件，但不會自動取代正式文件核准。

`GET /livez` 是不碰依賴的程序探針；`GET /readyz` 檢查 schema v6 與全域 worker lease。`GET /health` 再加入載入 commit、工作區 commit、程式碼指紋、SQLite quick check、ffmpeg/ffprobe、本機與異地備份健康度及 DB／媒體／備份／可用磁碟容量門檻。大型快照驗證以檔案 identity 快取，並以排除 runtime lease/FTS cache 的 durable record-state 指紋判斷能否重用；ZIP/SQLite 可讀但 manifest 有缺檔時明確標示復原不完整並降級。

### 5. 治理與維運模組邊界

| 模組 | 責任 | 禁止跨越的邊界 |
| --- | --- | --- |
| `backend.auth` | 身分、角色、中央路由權限政策 | 不接受用戶端自報角色 |
| `backend.review_workflow` | 文件與 D/R/A 逐項狀態轉換、回捲規則 | 不處理 HTTP 或畫面 |
| `backend.evidence` | 附件複製、分析與提交 | 未完成資料庫交易前不得留下孤兒附件 |
| `backend.maintenance` | 一致性備份、記錄快照、驗證與安全還原 | 還原不得覆寫非空目錄 |
| `backend.confirmation_queue` / `confirmation_api` | 歷史 D/R/A 回填後的缺漏確認、解決與 UI API | 不猜測負責人、期限或時間碼 |
| `backend.structured_minutes` | 規則式解析既有標準 Markdown D/R/A | 只回填原文存在的欄位 |
| `backend.capacity` | DB、媒體、備份與磁碟容量健康門檻 | 不做自動刪除 |
| `backend.database` | schema、交易與查詢 | 不反向依賴 review domain 或路由授權 |
| `backend.schema_migrations` | 可回滾 schema 升級、FK/CHECK 與孤兒事件封存 | 不反向依賴 API 或 database facade |
| `backend.access_tokens` | 短效 bootstrap 與簽章 session capability | URL/cookie 不保存原始 API key |

營運查詢都有明確上限：任務延遲與錯誤分類最多讀取最近 1,000 筆，API 清單維持分頁上限；若長期工作量超過單機 SQLite／單 worker 的容量，再以觀測到的 p95 與 queue depth 作為升級外部佇列或 PostgreSQL 的依據。

### 6. P0～P3 驗收矩陣（2026/07/30）

| 優先級 | 驗收範圍 | 完成證據 |
| --- | --- | --- |
| **P0** | 資料完整性、任務血緣、備份一致性、權限邊界 | schema v6 FK/CHECK、原子 meeting/job commit、generation fencing、本機/API session RBAC、全 mutation audit |
| **P1** | 結構化逐項審查與佐證鏈 | 28 筆歷史 D/R/A 回填、逐項 API/UI、`meeting_confirmation_tasks`、`meeting_evidence_items` |
| **P2** | 可觀測性與還原能力 | live/readiness 分離、queue/attempt/lease 指標；本機＋異地 v2 快照、缺檔降級語意、可執行 runtime 還原 |
| **P3** | 維護邊界與容量限制 | review/database 循環移除、全 backend cycle guard、函式／行數守門、容量門檻、bounded restart、依賴弱點掃描 |

---

## 四、標準資料流向 (Data Flow)

以**一次線上會議**的完整流程為例：

```mermaid
sequenceDiagram
    participant U as 使用者桌面工具
    participant API as FastAPI 後端
    participant PROC as 預處理模組
    participant TX as Gemini 轉錄
    participant SUM as 摘要模型
    participant DB as 儲存層

    U->>API: POST /upload-media (媒體檔)
    API-->>U: {"status": "processing"} 立即回應
    API->>PROC: 存入 Temp，格式檢查
    PROC->>TX: 分段上傳與轉錄
    TX-->>PROC: 含時間戳逐字稿
    PROC->>PROC: 本機品質閘門與必要補救
    PROC->>SUM: 完整逐字稿 + JSON contract
    SUM-->>PROC: 結構化 D/R/A
    PROC->>DB: 寫入 Markdown + SQLite + FTS
    PROC->>PROC: 刪除 Temp 媒體檔
    PROC-->>U: 推播「處理完成」通知
```

---

## 五、開發里程碑 (Roadmap)

| 階段 | 功能 | 狀態 |
|------|------|------|
| **Phase 0** | 單檔處理 CLI 腳本（MVP） | ✅ **已完成並驗證** |
| **Phase 1** | FastAPI 後端 + `/upload-media` 端點（`/upload-audio` 相容舊整合） | ✅ **已完成** |
| **Phase 2** | 桌面錄音 GUI（sounddevice + Tkinter） | ✅ **已完成** |
| **Phase 3** | LINE Bot Webhook 整合 | 🗑️ **已移除（v2.5）** |
| **Phase 4** | SQLite 歷史記錄 + 搜尋功能 | ✅ **已完成** |
| **Phase 5** | 雲端部署（Render / Fly.io） | 🔲 待開發 |

---

## 六、技術選型清單

| 類別 | 套件 / 服務 | 用途 |
|------|------------|------|
| AI 核心 | `google-genai` | Gemini 3.1 Flash-Lite API |
| Web 後端 | `fastapi` + `uvicorn` | API 伺服器 |
| 音訊擷取 | `sounddevice` / `pyaudio` | 麥克風 / 系統聲音擷取 |
| 音訊格式 | `pydub` + `ffmpeg` | 格式轉換、切割 |
| GUI | `tkinter` / `PyQt6` | 桌面錄音視窗 |
| 資料庫 | `sqlite3`（標準庫，免安裝） | 會議記錄元資料 |
| 環境管理 | `python-dotenv` | API Key 管理 |

---

*AI 語音會議助理 · 系統架構文件 v2.5 · 2026/07/30*
