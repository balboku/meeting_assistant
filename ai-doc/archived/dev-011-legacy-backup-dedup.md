# DEV-011 既有備份去重遷移

2026-10-22 完成資料遷移，後續工具部署結果見「工具與文件正式部署」節。使用者明確選 A：新副本完整驗證及逐份還原通過後，永久替換精確五份本機舊 ZIP；不刪原始錄音與會議紀錄。獨立後續交付，不回算原六項品質升級完成率。

## 最終結果與資料保全

- 五份全部完成，永久替換收據 replaced-0..4.json。兩份日常 ZIP 原子替換於原路徑；三份發版／修復薄 ZIP 移至同一備份根目錄，原目錄保留 deduplicated-snapshot.json 映射及所有既有歷史 SQLite／收據／獨立影音。沒有送回收筒，不能從回收筒復原舊容器；各日期內容仍可由去重版還原或用既有匯出工具產生完整 v2 ZIP，不承諾舊 ZIP 封裝逐位元重現。
- 本機受管容量在收據觀測點由 19,087,248,080 降至 4,408,748,432 bytes，減少 14,678,499,648 bytes（13.670 GiB，約76.9%）；顯示17.8→4.1GiB。後續收據及服務存取日誌略增，前端截圖時4,408,772,495bytes，非固定不變數字。API／檔案系統採前後bookend，非發版分類逐一相等，增量只允許實測日誌增量。全磁碟空閒觀測增加27,346,161,664bytes，包含測試清理等其他背景變動，不能全歸因本次；節省量以受管備份差額計算。
- 共用獨立池65物件／3,998,469,035bytes，五份薄 ZIP 合計17,191,105bytes。逐份637個 entry CRC／SHA、內嵌DB完整性／FK、完整歷史manifest等價（僅format／shared mapping可變）及實際隔離還原均通過。歷史會議數按原清單為62／67／63／62／67；raw DB SHA精確相等，runtime影音／Markdown、各表非路徑完整資料列與重複數、jobs payload非路徑值均相同。
- 最終再驗13,499個正式輸出與歷史DB逐檔SHA未變；68會議／70任務／0課程分段／active0，全部jobs rows含attempts與設定均未變。持久資料SHA 63020031110203ec00a18e0230a6cb5817b43524df13f160e092fbc7ab04f57f，任務列SHA 6feceb90bc37230cddeeac3d9a70de3f40bc504f2c4709707244161ed71f4919。78個正式原始檔、備份目錄內的獨立影音、歷史SQLite與原回收筒均未刪。
- 65個物件與相同內容正式來源逐一確認不是hardlink。異地最新原 ZIP 全檔SHA與已驗證本機來源相同，涵蓋65影音；異地檔案stat前後一致，未轉換或寫入異地。原缺6個來源的 degraded ZIP 保留原樣，不宣稱修復缺檔。
- 正式產品仍757bce5，工作副本乾淨；未重啟／部署、改schema／權限／模型／設定／容量門檻，未呼叫模型或重跑會議。ready／worker正常，health全部ok，載入提交／來源匹配且auth啟用。日常保留兩份；三份專用根目錄前綴不是 meeting_records_*.zip，實測不落入日常輪替。沿用v3，不自動GC。

## 專用快照的新位置

以下均相對正式 backups/；必須和同根目錄 media_objects/ 一起保留：

- hotfix_20261015_speech/meeting_records_20261015_145542.zip → hotfix_20261015_speech__meeting_records_20261015_145542.zip
- release_20261013_quality/meeting_records_20261013_131357.zip → release_20261013_quality__meeting_records_20261013_131357.zip
- release_20261021_training/meeting_records_20261021_182339.zip → release_20261021_training__meeting_records_20261021_182339.zip
- 日常 meeting_records_20261013_090249.zip／meeting_records_20261021_183054.zip 路徑不變，內容格式改為v3。

## 驗證、未通過紀錄與復原

- 新增離線 scripts/deduplicate_record_snapshot.py 只產生全新v3、不刪來源；拒絕覆寫、降級來源、未列內容、重複member、路徑及容量不符、損壞物件。配套11項測試含v1相容、v3拒轉、實際還原及來源獨立；完整回歸791 tests／273.570秒，architecture／security含untracked／diff通過，未放寬主檔上限。初次沙箱全套停在既有health案例，精確停止本輪測試子程序，正式服務不動；同原碼於可用本機socket環境通過。
- 首次runtime驗收helper假定rowid，SQLite自建FTS shadow表實際WITHOUT ROWID而失敗；產品還原已完成但驗收不算通過、舊ZIP保留。改全欄位Counter多重集合保留重複數／BLOB後重驗，五份均過。另6項虛構正負案例覆蓋漏重複列、改BLOB／業務payload／品質metadata；fixture首跑Windows鎖檔，explicit closing後6項／0.649秒通過，未放寬資料門檻或修改產品還原器。
- 首次Windows原子呼叫把PowerShell $null綁成空字串，File.Replace拒絕備份路徑，當時舊ZIP替換數0、共用池已移入，暫增受管容量；未調容量門檻。隔離tiny fixture證明[NullString]::Value可正確傳入null；重新驗完整移入池65物件、五個舊ZIP SHA及正式state guard才接續，之後五份全部成功。native-replace-first-failure.json與逐份成功收據並存，不抹除失敗。
- 只刪本輪五個已解析、完成還原證明的disposable restore_0..4副本，非正式原始檔；不遞迴刪 staging根目錄、既有備份資料夾或回收筒。此批不可逆的是舊容器，非快照歷史資料；新v3可還原／匯出，異地舊完整副本亦保留。

## 正式前端與證據

- 原Chrome分頁135044164，同一LAN /static/index.html。reload呼叫曾回CDP dispatch deadline、結果不確定，未據此宣稱成功；重新觀察同一分頁確認68會議、70任務、78原始檔／4.7GiB、備份4.1GiB及維運正常，沒有換新頁抹除失敗。
- 1920×842，唯讀明細各分類與精確bytes清楚，無可見inline-error／alert、水平overflow／裁切，console warn／error0。關閉後focus回ops-backup-storage-tile；保留原正式頁。未改UI，以本次現有desktop surface驗證數據；同產品既有三viewport證據重用，不宣稱重做手機版或模型品質驗收。
- 正式 backups/dedup_20261022_5zip/ 含authorization、baseline、online DB、ci-791.log、offsite-readonly、converted-0..4、restored-0..4、harness負向及失敗證據、cutover-plan、replaced-0..4、preservation-final、smoke。隱私指紋、設定摘要及會議資料只留ignored本機。
- UI開發 tmp/legacy-dedup-ui-20261022/backup-detail.png、checks.json、after-close.json；本次helper及staging皆ignored。資料遷移完成當時產品工具／測試／README／任務與歸檔文件尚未commit／push；後續Git交付見下節，不更新正式程式。後續異地舊ZIP去重、自動GC或其他刪除未納入；需另有明確範圍確認。

## Git交付邊界（2026-10-22 後續指令）

使用者另要求「commit／push 這次既有備份去重的工具與文件」。本次提交僅含 scripts/deduplicate_record_snapshot.py、tests/test_snapshot_deduplication.py、README.md、dev_task.md、documentation_map.md 與 DEV-010／DEV-011 歸檔，共七檔，推送既有 feat/meeting-quality-upgrades 分支。正式副本仍757bce5；不部署、不重啟、不重跑遷移或會議，不提交正式備份、影音、設定、收據與ignored helper。

提交前確認轉換工具SHA與實際執行留存副本一致；既有791項全套證據對應同一版工具／測試。另複跑27項去重及共用備份測試，46.357秒通過，architecture／security（含untracked）通過。新證據 tmp/dedup-git-focused-20261022.log 僅留本機。Git提交／推送成功與否以當次交付回報及遠端分支核對為準，不把提交視為正式部署。

## 工具與文件正式部署（2026-10-22）

- 使用者後續明確要求部署這次工具與文件，不重跑既有會議、不刪原始錄音及會議紀錄。候選2869b3a995b585ba146aeb0f4070008d0337d224已push開發分支；正式feat/transcript-chinese-translation自757bce5安全fast-forward，僅前述七檔，無backend／UI／schema／依賴／權限／模型／設定調整。不執行正式轉換器、資料遷移、備份替換、保留規則或費用變更。
- 先核對active0、基線health／ready，新增小型online DB與設定備份、原始輸出及歷史DB全量指紋；不新增大型影音ZIP。回復方案為保留資料、以基線建立獨立本地回復分支及重啟，不hard reset或還原正式DB。任務／資料變動、髒工作區、程序身分不符、保全或健康失敗即停止驗收。
- 來源指紋含scripts，故需短暫重啟更新載入版本。預先確認原啟動流程會同步異地與清理；部署期間用既有meeting-assistant-startup-maintenance互斥lease（15分鐘TTL）保留此輪工具限定邊界，不改queue lease／保留設定。隔離合成DB驗證「其他startup owner無法取鎖、queue獨立、可正常釋放」後才操作正式。精確核對四個原supervisor／uvicorn PID及建立時間後停止，隱藏啟動原start.py；新PID100、16:01:51 ready、16:02:42 leader generation58。啟動日誌確認略過重複維護，服務就緒即釋放本輪lease，不遺留部署鎖；正常後續維護機制不變。
- 重用同工具／測試791項全套與提交前27項證據，正式副本另跑27 tests／23.949秒／OK，使用隔離暫存DB及假金鑰，不呼叫模型。部署CLI --help exit0、LAN /static/index.html HTTP200；七檔與候選提交逐一核對一致。ready／worker／health全項ok、auth啟用、載入提交及來源指紋與工作區相符；無UI變更，不宣稱重新執行三種viewport或實際辨識品質驗收。
- 最終13,500個基線輸出／歷史DB大小與SHA全部未變；68會議／70任務、active0、全部jobs rows含attempts／設定均不變。持久資料SHA63020031110203ec00a18e0230a6cb5817b43524df13f160e092fbc7ab04f57f、jobs SHA6feceb90bc37230cddeeac3d9a70de3f40bc504f2c4709707244161ed71f4919。226個既有非啟動日誌備份及13個異地檔stat均不變；兩個原啟動日誌允許本次GET存取紀錄增長，新發版備份／收據另列。不刪任何原始影音／會議／備份，不重跑或新增任務。
- 首次保全集合全等驗收失敗：多出Word匯出及seekable播放衍生檔兩個，無缺檔／大小變動。舊服務存取日誌2940、2943–2945及兩檔16:00:35建立時間證明，是其他LAN客戶端在新服務16:01:51啟動前對第68份的匯出200／播放206操作，非部署重跑。保留失敗收據及兩檔，不刪除或重設基線；驗收helper改為每個原基線檔案精確SHA不變，僅允許這兩個確定路徑／SHA的新增檔，未知新增仍失敗。再次全量驗證通過，13,502個現存受保護檔案stat於最終smoke仍一致；未修改產品／測試原碼或豁免原始檔保全。
- 備份健康非同步暖機曾回degraded／完整驗證進行中，ready已正常，保留startup-first-observation.json；一次後續smoke亦碰到60秒refresh而未通過，不當成正式故障修產品或跳過檢查。完整驗證完成後最終smoke全項ok。完整SHA驗證與最後健康採分離證據、檔案stat bookend，避免長時間hash跨刷新週期重跑同一全量步驟。
- 證據留正式backups/release_20261022_dedup_tool/，含baseline、online DB、.env.before、deployment-plan、lease取得／釋放、stopped-processes、startup logs、artifact-tests.log、首次觀測／保全失敗、observed-additions、preservation-after與smoke。實際工具2869b3a正式工作副本乾淨；發版後僅在開發分支本地提交本節及兩份入口更新，不額外push正式／開發分支、不再部署或重啟；ignored helper與私人收據不推Git。

## 原始契約與階段記錄

以下保存執行前契約及中途證據，描述「尚未」者為歷史狀態；目前狀態以上述最終結果為準。

### DEV-011 既有備份去重：唯讀盤點與確認邊界（2026-10-22）

使用者已要求處理舊備份，但未選擇舊 ZIP 的永久替換或回收筒保留方式。依 dev-pm 正式資料遷移／刪除 gate，先盤點和驗證；尚未轉換、覆寫或刪除。此為 DEV-010 原 future capsule 的啟動，不宣稱已降低容量。正式產品仍為 757bce5，無服務重啟或部署。

後續人類決策：使用者明確回覆「同意 A，請在去重版完整驗證及還原測試通過後，永久替換這 5 份舊 ZIP，不要刪除原始錄音與會議紀錄」。此確認只涵蓋下表五份本機舊容器，不包含正式資料、歷史獨立 DB、降級備份、異地或回收筒。RD Contract Ready／RD Implementation Ready，實作離線新副本轉換器；沿用已部署 v3 及原替換邊界，無新格式或 API／權限／schema 變更，無需另建 ADR。

執行補充：先在開發 ignored tmp 專用 staging 根目錄共用一池，五份完成驗證及實際還原後，再將已驗證池與薄 ZIP 移至正式備份根目錄，避免長時間暫增受管容量。原始輸出、會議／全部任務列、設定及非候選備份建立本輪全量指紋；異地既有最新 ZIP 只讀核對與已驗證來源相同，確認完整影音可讀。僅清理本輪新建的隔離還原副本，並保存每 entry 驗證收據。被永久替換的舊 ZIP 不進回收筒；歷史內容仍可由 v3 還原及匯出完整 ZIP，但不承諾重現舊 ZIP 容器的逐位元封裝。

建議精確範圍為正式 `backups/` 下五份完整 ZIP，合計 18,706,320,777 bytes：

| 相對路徑 | bytes |
|---|---:|
| meeting_records_20261013_090249.zip | 3,669,482,576 |
| meeting_records_20261021_183054.zip | 3,848,078,999 |
| hotfix_20261015_speech/meeting_records_20261015_145542.zip | 3,671,198,956 |
| release_20261013_quality/meeting_records_20261013_131357.zip | 3,669,482,579 |
| release_20261021_training/meeting_records_20261021_182339.zip | 3,848,077,667 |

- 盤點：上述五份的影音共用 65 個 SHA-256／3,998,469,035 raw bytes；加上非影音壓縮內容約 4,015,542,050 bytes（未計新 ZIP header）。初估淨減 14,690,778,727 bytes，約 13.7 GiB；本機受管備份約 17.8→4.1 GiB，實際以遷移後核對為準。
- 不動範圍：正式 output 下所有原始影音、逐字稿、會議文件與附件、正式 DB、歷史獨立 SQLite、既有發版收據和獨立影音檔、原回收筒及異地備份。`degraded/meeting_records_20260813_143419.zip` 原缺 6 個來源，保留原樣，不偽裝成補齊。異地僅讀 manifest；整批約有 8.6 GB 的潛在重複，不納入此次建議替換清單。
- 待確認方案：A＝新副本全量驗證及隔離還原通過後永久替換上述五個舊 ZIP 容器，實際釋出空間；B＝舊 ZIP 送回收筒，可復原但不釋出實體空間，建立約 4 GB 物件庫還會暫增實體占用；C＝停止於盤點。原始與會議內容在各方案均不刪。
- 工程方向（待處置確認）：沿用 v3，不改格式／API／schema；由舊 ZIP 直接串流轉換，不從目前 DB 重建歷史。日常兩份沿用原路徑；三份專用薄 ZIP 以 `release_...__meeting_records_...zip`／`hotfix_...__meeting_records_...zip` 前綴存於同一備份根目錄，與日常快照共用一個 media_objects/。專用前綴不符合日常 meeting_records_*.zip 輪替；舊目錄保留映射收據，舊發版證據不重寫。不用跨目錄 junction／硬連結原始影音。
- 必要驗收：先以隔離合成資料驗證轉換及失敗保全；正式逐份 CRC／所有 entry SHA／內嵌 DB 驗證，新舊邏輯 entry、完整 manifest（僅格式和共用映射可變）逐一相等，五份各做隔離還原並核對歷史資料及影音。共用池須獨立副本，錯誤既存物件拒絕覆寫；發布不得覆寫未知目標。完成後再次核對正式原始檔、會議／全部任務列及設定不變，確認容量與 ready／health。不呼叫模型、不重試會議、不自動 GC、不 push。
- 停止條件：來源或正式資料變動、處理中任務、空間不足、目標衝突、路徑越界／連結、缺檔／SHA 或還原不一致、無法確認異地既有影音可讀。任何不完整的新副本不得取代舊 ZIP；中斷保留已驗證物件及收據，不擴大刪除。
- 只讀檢查途中：ready／worker 正常，68 份會議、70 筆任務、0 課程／分段、active=0；不是前一輪的 67／69，後續必須新建本輪保全基線。當前僅盤點 helper（ignored tmp）及此處續接文件有變更，尚無轉換產品程式、永久刪除、回收或異地寫入。
- 唯讀 QC 完成：五份候選均通過現行 verify_record_snapshot 的全 entry CRC／SHA-256 及內嵌 SQLite 完整性檢查，缺檔皆 0；另逐份取得 ZIP SHA-256，讀取前後 size／mtime 一致。正式持久資料 SHA 在檢查前後均為 63020031110203ec00a18e0230a6cb5817b43524df13f160e092fbc7ab04f57f；未做轉換或還原演練，不能算遷移完成。完整私有收據 tmp/legacy-backup-inventory-20261022_141856.json，異地 manifest 盤點為先前 _141719.json；均 ignored、不推 Git。首次只讀狀態命令因 PowerShell 引號錯誤未執行，改為工作區 helper 後 ready／counts 查詢成功；未變更正式系統以規避環境限制。
- A 方案執行 QC：離線轉換工具及隔離負向測試已新增，完整回歸791 tests／273.570秒、architecture／security（含未追蹤檔）／diff通過；主檔原行數上限不變。正式本輪新基線13,499個受保護輸出／歷史 DB，68會議／70任務（全部列含attempts）／active0，另存小型online DB，不新增完整影音 ZIP。異地既有最新 ZIP 逐位元 SHA 與已驗證本機來源相同，涵蓋全部65個影音，無異地寫入。證據集中正式 backups/dedup_20261022_5zip/。
- 五份暫存去重 ZIP 均通過 CRC／entry SHA／內嵌 DB、完整歷史 manifest 等價檢查；同一獨立池65物件／3,998,469,035 bytes，薄 ZIP 合計17,191,105 bytes。五份各實際還原至開發專用 staging，逐一驗637個 entry、raw DB 全檔 SHA、runtime影音／Markdown、各表完整非路徑資料列多重集合及 jobs payload 非路徑值；歷史會議數依清單為62／67／63／62／67，均通過。還原副本非正式原始檔，待最終 gate 通過後僅清理這五個已解析的 disposable restore_0..4 目錄，不清理 staging 根目錄或其他暫存。
- 未通過證據保留：首輪沙箱全套停在既有health案例，精確辨識並停止本輪測試子程序，非正式服務；同原碼在可用本機socket環境重跑791項通過。首次 runtime 驗收 helper 假定全表有rowid，SQLite自建FTS shadow表實際WITHOUT ROWID而失敗；產品還原本身成功，未刪舊ZIP。改為全欄位Counter多重集合（保留重複數、BLOB）後重新全量核對第一份並完成五份；追加6項獨立虛構資料正負案例。該fixture首跑在Windows因sqlite context不自動close造成cleanup鎖檔，加入explicit closing後6項／0.649秒通過；未修改產品還原器或放寬資料門檻。收據保留restore-harness-first-failure.json、gate-harness-tests.log及gate-harness-tests-retry.log。新工具預設不刪來源，實際永久替換腳本缺少明確execute switch時拒絕，已驗證無檔案操作。
