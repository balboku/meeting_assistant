# 會議品質升級工程契約

使用者決策：除了日曆自動化，其餘比較項目執行。沿用免費模型、匿名發言人，不推定真名。

## 總任務清單

1. 🟡 DEV-001 收音完整性｜實作與模擬驗證完成；實機驗收待辦
   - 交付：Windows 一般桌面錄音可收系統音訊與麥克風，來源獨立儲存、可見音量與錯誤；瀏覽器入口給出可操作的收音確認。
   - 下一步：在同仁實際使用的播放裝置與麥克風完成一次錄音／回放驗收；EXE 不需特製 Chrome。
   - 驗收：停止/裝置失敗/磁碟寫入失敗可恢復，不能把未收音顯示為成功。
2. 🟡 DEV-002 整場匿名發言人｜整合完成；實際會議準確率待驗收
   - 交付：本地聲學分群，全場代號一致；重疊或證據不足標示未知。只存時間與匿名代號，不存跨場聲紋。
   - 下一步：以人工標註的實際會議計算 DER；目前只完成公開四人樣本及對齊演算法驗證。
   - 驗收：文字與時間不變、跨片段同人可一致、缺模型可見降級原因。
3. ✅ DEV-003 共用詞彙與修正回饋｜實作與隔離驗證完成
   - 交付：人工提出/核准/停用術語，後續工作保存詞庫快照，既有紀錄沿用版本修訂流程。
   - 下一步：正式發版後由管理員建立第一批核准詞彙；此輪不寫入正式詞庫。
   - 驗收：未核准詞不自動使用，重試不漂移，viewer 不能寫，變更可追溯。
4. ✅ DEV-004 跨會議證據問答｜實作與隔離驗證完成
   - 交付：問題檢索、多份會議引用、原文/時間/版本/審查狀態、可開啟來源；無證據明確回答不足。
   - 下一步：正式發版後核對正式會議的檢索覆蓋；本地原文不需模型，雲端為可選摘錄挑選。
   - 驗收：引文必須存在來源；未核准內容不冒充正式決議；無結果不編造；機密不外送。
5. ✅ DEV-005 資料外送控制｜實作與隔離驗證完成
   - 交付：每場一般/機密選擇與部署層禁止雲端設定；機密使用本地處理，缺本地資源在接收前明確提示。
   - 下一步：部署時安裝本地模型與選定政策；本地只提供逐字稿，摘要／決議／待辦仍需人工整理。
   - 驗收：所有機密路徑零雲端呼叫；不啟用付費帳單。
6. 🟡 DEV-006 可重現品質基準｜評分工具完成；真實標準答案待建立
   - 交付：人工逐字稿對照的中英文錯字率、術語/數字保持率、發言人錯誤率與回歸門檻。
   - 下一步：以人工聽寫建立中文／英文／混語等資料；不把合成案例視為 ASR 準確率。
   - 驗收：標準答案與預測分離；匿名代號置換不扣分；錯誤會使檢查失敗。

## 範圍與依賴

- 工程選擇：以本地 CPU 推論支援目前 16 GB / 8 邏輯核心 Windows 主機。模型為可選元件，安裝與狀態可檢查，不在伺服器啟動時自動下載。
- 錄音與隱私偏好已提出非阻斷問題；預設實作可選桌面收音與一般/機密兩種處理路徑，保留既有流程。
- 不在此輪重跑正式會議或重新寫入既有內容；不做日曆、真名辨識、跨會議聲紋。
- 既有主檔行數上限維持；功能存於獨立 module/router/assets。

## UI 與 QA 契約

- 入口：「會議知識庫」，主要任務為輸入問題查找來源；詞彙修正為次要頁籤，錄音模式在錄音入口。
- 狀態：載入中顯示等待；空資料提示加入會議/詞彙；失敗可重試；無權限可讀不可改；來源引用顯示審查狀態與時間。
- 桌面 1440×900、低高度 1024×768、手機 390×844 實際瀏覽器檢查。無水平溢出、裁切或看不到的主要按鈕。
- 隔離測試涵蓋權限、詞庫快照、引用真實性、機密阻擋、模型缺失、分群對齊、錄音串流失敗與基準演算法。
- 證據：測試指令/結果及 UI 檢查記錄於本文件；未執行的硬體錄音與人工準確度明列未驗證。

## QC 證據（2026-10-13）

- 隔離全套 `python scripts/run_ci_tests.py`：701 tests / 190.470 秒 / OK，記錄 `tmp/quality-ci-pass.log`。之後補上的空分群防錯另跑 speech + desktop 12 tests / OK，記錄 `tmp/speech-final.log`。
- 權限／本地工作／機密來源／詞庫快照／續跑重用／引文測試：57 tests / OK，`tmp/targeted-final.log`。測試 Client 為注入替身，不使用實際模型額度。
- 最後補驗機密補件、缺模型接收前阻擋、舊 CLI 外送阻擋及空分群：knowledge + desktop 35 tests / 4.527 秒 / OK，`tmp/knowledge-final.log`。
- `check_architecture.py`、`security_scan.py --include-untracked`、`git diff --check`、`pip check`、新 JS syntax 及原 dashboard/minutes-reader JS contracts 通過。主檔行數 tasks 10981/11000、main 4204/4450、database 4356/4380、index 7466/7510。
- `evaluate_speech.py .../speech_cases.json` 通過，reference 明確標為合成演算法案例、非真實音訊準確度。
- 本地模型實跑：官方 `0-four-speakers-zh.wav`（56.86 秒）在預設 threshold 0.9 辨識 4 位，耗時 25.89 秒。Whisper base int8 轉錄 14 秒公開片段耗時 9.77 秒，仍有詞彙錯誤；不能宣稱實際公司會議已達指定準確率。
- 桌面：`build_desktop_recorder.py` 完成；打包後 `MeetingRecorder.exe --self-test` exit 0（只載入依賴與列舉裝置，沒有啟動錄音）。ZIP 28,531,543 bytes；SHA-256 `F97A92CE90E34BDD98E1DB9AC5B0052DD471A205FC0949C58C8233CE2C8382E2`。原始軌、FLAC seek、裝置失敗、未收音、寫入失敗及合併重試已用注入音訊測試；未做真實硬體收音／原生 Tk 視覺驗收。
- Web 實測：1440×900 知識查詢含 2 筆來源；390×844 詞彙提出／核准 dialog、錄製模式與按鈕；1024×768 詞彙頁、螢幕錄製 dialog。檢查無水平 overflow、主要按鈕在可視區域；未核准來源過濾、無結果、雲端禁用提示、來源深連結與無來源音檔狀態均已核對。
- 修正 QA 發現：`/meetings` 回傳 records 包裝、不保留 query 的 `/history` redirect、長段尾端發言人混用、核准詞被舊修正規則覆蓋、metadata 寫入失敗前已啟動音訊，以及 partial FLAC 發布問題。
- 重新啟動隔離預覽後，capabilities 回報本地模型就緒／cloud=false；桌面 ZIP ranged GET 回應 206、`bytes 0-15/28531543`，檔案可由新版 API 取得。下載路由僅接受 GET，不提供 HEAD。
- 截圖：`tmp/knowledge-desktop.jpg`、`tmp/knowledge-mobile-dialog.jpg`、`tmp/knowledge-low-height.jpg`、`tmp/recording-mobile.jpg`、`tmp/recording-low-height.jpg`。最後重新保存知識庫畫面時瀏覽器工具連續逾時；先前截圖已保存，但測試 viewport 還原未能確認。
- 預覽限本機 `127.0.0.1:8018`，假 key、獨立 DB、禁止雲端且 worker/lifespan 關閉。正式分支仍為 `43d4e38` 且工作目錄乾淨；未 commit／push／部署，未重跑或修改正式會議。

## 交付與下一步

程式實作已交付於獨立 `feat/meeting-quality-upgrades`。操作／安裝詳見 [meeting-quality-upgrades.md](../docs/meeting-quality-upgrades.md)。2026-10-13 使用者已授權 commit／push／部署。準確率與實機收音的驗收不計為已完成，也不自動擴張到日曆、付費服務、真名辨識或跨場聲紋。

## 發版契約與證據（2026-10-13）

- 目標：現有 Windows 正式副本 `meeting_assistant`、port 8001；沿用 `start.py` supervisor、身份驗證、LAN 範圍與 `.env`。基線 `43d4e3845d2d645db0676ab80614fb5174065479`；正式分支 `feat/transcript-chinese-translation`，以 fast-forward 接收本輪功能分支，再同步兩個遠端分支，不改 main。
- 發版範圍為上述六項、必要 runtime、已驗證的本地模型與桌面 ZIP。三個新增資料表延遲建立，不執行正式資料回填、既有會議重跑、權限資料遷移或付費服務啟用。
- 前置：正式目錄乾淨；62 份會議、64 筆任務（62 done、1 failed、1 cancelled），無 pending/processing。既存 failed 任務不在本次部署中重跑。
- 重新執行全套 705 tests / 108.404 秒 / OK（`tmp/release-ci.log`）。之後發現並修正新詞庫／詞庫事件／機密來源未納入備份內容變動雜湊；maintenance 10 tests / 1.470 秒 / OK，新增案例分別驗證三表與備份一致性。architecture、security（含新檔）及 diff check 通過。
- 備份：使用獨立 `backups/release_20261013_quality/`，SQLite online backup + 完整媒體／Markdown 快照，避免清除既存備份。保存 baseline 與完整性驗證 JSON；切換前後核對會議／任務／修訂／權限資料及所有 output 檔案與 `.env` 的 SHA-256。
- 模型與桌面 ZIP 由隔離驗證副本複製，逐檔核對 SHA-256；runtime 使用 `requirements-local.lock --require-hashes --no-deps`，不替換主依賴。
- 停止條件：出現 active job、來源資料變動、備份不完整、秘密掃描失敗、runtime 載入失敗或身份驗證回歸。切換後檢查版本、worker、會議數、capabilities、只讀詞库、本地原文查詢、Range 下載與資料保全。
- 回復：若新版無法提供服務，在正式工作目錄乾淨且沒有新工作時切換到已記錄基線 `43d4e38` 並重新啟動既有 supervisor；不使用 hard reset、不還原或刪除資料庫。新增 runtime／models 可保留且舊版不使用；若涉及正式資料恢復先停止並請使用者決定。
- 基線健康已為 degraded：database_backup／offsite_record_snapshot 顯示「完整驗證進行中」，既有快照可恢復、缺檔 0。不得把這個既有狀態誤報為新版故障或宣稱所有健康項已通過。
- 本輪瀏覽器控制工具逾時。重用前輪隔離實測截圖；正式站畫面複驗尚未通過，不以 API 成功冒充 UI 實測。硬體收音／準確率／unsigned EXE 的限制保留。
- 最終候選全套（含備份修正）706 tests / 152.107 秒 / OK，`tmp/release-final-ci.log`。產品提交 `22e677a1b273f6b77d49e9e0bccdbdec0bbb5207` 已推送 `origin/feat/meeting-quality-upgrades`；後續文件提交不改變產品來源指紋。
- 正式主機已安裝鎖定的 sherpa-onnx／core 1.13.8、soundcard 0.4.6，`pip check` 通過；9 個模型／來源授權檔及桌面 ZIP 與測試副本 SHA-256 全部一致。保留原 `.env` 與原主依賴。
- 部署收據位置：正式副本 `backups/release_20261013_quality/backup-verification.json`（完整快照驗證）、`preservation-check.json`（前後資料保全）、`smoke.json`（實際載入提交、worker、API／下載與剩餘健康警示）。這些含環境路徑的操作證據留在受管主機、不送入 Git；僅在收據全部通過後回報部署完成。正式站 UI 複驗與上述人工驗收為保留待辦，不以收據的 API 通過取代。

使用思考習慣：#拆解問題、#限制條件、#可驗證性
