# DEV-010 備份去重與容量分流

2026-10-22，本地實作與驗證完成。來源：使用者要求減少備份重複副本、不刪原始錄音及會議，修正前端容量單位。獨立交付點，承接 DEV-009，不回算原六項品質升級完成率。本輪不包含 commit／push／部署、正式資料遷移或備份清理。

## 固定架構與 RD 契約

- 新建預設 v3：DB、Markdown、附件、前次文件、manifest 存於 ZIP；影音存於同備份根目錄 `media_objects/<sha前2碼>/<sha>`，同內容跨快照共用。不同根目錄獨立，不宣稱跨所有發版子目錄去重。無 DB schema、權限、金鑰、費用或警示門檻變更。
- ZIP 與同目錄物件庫是一組完整備份。共用物件損壞影響多份引用快照，因此異地保有獨立檔案，不硬連結原始影音。v1／v2 可還原；`shared_media=False` 可建立 v2。`scripts/export_record_snapshot.py` 可匯出驗證過的單一 v2 ZIP，拒絕覆寫且需要完整額外容量。
- 建立／重用／還原／異地複製均驗 SHA-256 與容量；影音及完整 ZIP 先寫唯一暫存檔，驗證後原子發布，不覆寫歷史。失敗不發布最新 ZIP、不輪替健康歷史。來源缺檔的可讀降級快照仍可保存，但不淘汰可用歷史。
- 快取包含物件 stat，缺檔／變更不沿用舊成功；損壞既存物件不自動覆寫。本機／異地原保留參數不改，發版專用子目錄不遞迴輪替。不自動 GC、不轉換／清理舊 ZIP，不操作正式影音及會議。
- `backend/snapshot_media.py` 管物件／原子發布；`backend/maintenance.py` 整合備份還原。`backend/backup_storage.py` 只讀掃描本機備份，`/metrics.storage.backup_storage` 回傳日常 ZIP／共用影音／發版修復／DB／其他互斥分類及總 bytes。不含原始影音、異地、回收筒；讀取不完整為 complete=false、total_bytes=null，非假0。

## UX 與驗收契約

操作者要在5秒內分辨影音不是系統總容量，下一步開唯讀明細，不增加清理 CTA。主畫面分「影音容量」（含已移除保留檔）與「本機備份容量」，明細列分類、bytes、單位及搬移限制。1024進位用 GiB／MiB／KiB，含錄製恢復與初始上傳提示；不把十進位GB和二進位GiB混用。沿用原始檔清單，dialog 支援關閉／Escape／焦點回入口；載入或失敗顯示未取得與重新整理路徑。手機容量入口至少44px。

驗收涵蓋跨快照去重、變更才新增、原檔／會議保全、舊格式及可攜匯出還原、異地獨立／缺檔補齊／損壞阻擋、快取失效、路徑安全、輪替保全、失敗不發布；容量分類／API整合／拒讀不可回0、JS單位與狀態、三種 viewport、互動、可見錯誤及數據核對。

## QC 證據

- 最終 `scripts/run_ci_tests.py`：780 tests／160.301 秒／OK，`tmp/shared-backup-final3-ci.log`。Windows Python3.14，隔離 TMP／TEMP、假金鑰，不呼叫模型、不重跑正式會議。原子發布改動先通過35項 focused，另加拒絕發布案例後再跑全套。
- architecture、security（含untracked）、diff、Node syntax／容量JS contracts通過；main4239/4450、index7479/7510，未放寬上限，依賴循環0。
- 隔離 `http://127.0.0.1:8019/` 唯讀API替身＋實際靜態資產，1440×900／1024×768／390×844：無水平overflow、可見錯誤或明細裁切。備份17.8GiB與19,074,709,197bytes一致，影音4.6GiB獨立；未取得／真正0B可區分，Escape焦點回入口，手機44px。替身資料不冒充正式站smoke。
- 收據 `tmp/backup-ui-qc-20261022/checks.json`；desktop-detail.png、low-height-detail.png、mobile-detail.png、mobile-dashboard.png、mobile-unavailable.png。檢查後還原viewport、關閉測試分頁及隔離預覽；含環境資訊的證據留ignored tmp。
- 未通過紀錄：初次沙箱全套在health案例停滯，終止且不算通過；另發現無效ZIP替身的single-flight案例失敗，已修正並通過7項針對性及後續全套。初始dialog受margin reset影響靠左上，已補margin:auto／按鈕樣式並重驗三尺寸，不以第一次畫面宣稱通過。
- 正式工作副本仍乾淨、產品提交未變；本輪未操作正式資料／備份／服務。開發保留dirty修改，未commit／push／部署，不宣稱既有19.1GB已下降。

## 後續邊界

共用release gate待使用者發版指令；上線前需確認異地檔案系統、足夠新物件／暫存空間，隔離恢復演練不取代正式異地實測。不預寫部署／rollback plan。停止條件：需刪除／覆寫正式備份、來源／會議，改權限／費用或降低完整性。

Future Phase Captured / Not Requested：舊完整ZIP去重遷移、物件GC、跨發版根目錄共用，須另有精確對象、容量／參照完整性分析、全量驗證及可復原方案，由使用者確認才執行，不擴大自動刪除範圍。
