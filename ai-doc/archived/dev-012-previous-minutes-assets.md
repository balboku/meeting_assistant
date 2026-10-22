# DEV-012 前次會議腳本404修復與Git同步

2026-10-22 完成修復、驗證及正式部署。來源為使用者要求修復JavaScript 404，驗證後commit／push／部署及完成Git收尾，禁止重跑既有會議、刪除原始錄音或會議紀錄。屬既有前次附件／補前次重產功能的開發點，不新增產品交付，不改DEV-001／002／006待人工驗收狀態。

## 根因與最小修復

- static/index.html原以/previous_minutes.js與/previous_minutes_rerun.js引用，正式僅掛載/static，兩個URL均404。原有測試亦寫死錯誤路徑，只查字串而未經應用路由。
- 產品只改兩行為/static/previous_minutes.js及/static/previous_minutes_rerun.js，更新原測試；新增tests/test_frontend_assets.py，解析首頁全部本機script，透過真實ASGI掛載檢查HTTP200、JavaScript MIME及檔案位元組一致，並確認runtimeConfig／formatBytes依賴先載入。沒有根目錄別名或backend／版型／schema／依賴／權限／模型／費用／.env變更。

## QA契約與結果

- 先重現兩個404及錯誤引用／依賴順序；首輪新測試2項含3個failures／1個error，屬修復前證據。受限環境另曾停滯，精確核對並只停止測試子PID12464，正式服務不動；在正常主機執行環境重現，保留red-retry紀錄。
- 修復後18項前次會議及資產測試／10.040秒通過。上傳／重產測試僅虛構資料及mock enqueue，沒有正式重跑。architecture、security含untracked、兩支JS語法、diff通過，既有大型模組行數上限不變。
- 首輪完整793項／350.465秒僅既有health回應時間1.179秒超過1秒；串行次輪793項／461.505秒僅既有GIL子程序等待退出案例未過。兩輪失敗並存，不改語音邏輯、測試門檻或略過案例。9項回應性／8.572秒及兩案例各3次（6項／23.424秒）通過後，同原碼最終完整793項／263.041秒OK。
- 實際正式副本再跑相同18項／7.539秒OK，使用專用暫存DB及假金鑰；沒有模型呼叫。正式HTTP兩支腳本均200且內容與部署檔一致，六個相對基線變更檔與候選Git內容逐一核對相同。

## 發版與資料保全

- 起始開發13edd17、正式2869b3a均乾淨。新修復1e871eff22164a6e5601688acdcdf9dcd7aab247已push開發分支，包含先前未推的13edd17；正式安全fast-forward，僅六檔（含前次發版文件），未混入私有資料。
- 先以正式active0、healthy／ready及新的68會議／70任務基線建立小型online DB／設定備份、13,503個輸出與歷史DB全量SHA；不新增大型影音ZIP。部署停止條件為資料變動、active job、髒工作區、程序身分不符或驗收失敗。回復方案保留資料，以基線的獨立回復分支重啟，不hard reset或還原正式DB。
- 使用既有startup-only維護lease避免本輪重啟意外同步／清理，僅停止已核對的四個supervisor／uvicorn PID。既有start.py隱藏重啟，新PID3036於18:38:23就緒、worker leader generation59；啟動日誌確認略過重複維護，就緒後立即釋放部署lease，沒有改queue lease／保留設定或留下部署鎖。
- 最終health全項ok、ready／worker正常、auth啟用、載入提交及來源指紋與工作區一致。13,503個基線檔案容量與SHA均不變，UI驗證後再次核對stat bookend；68會議／全部70任務含attempts及設定不變，active0。持久資料SHA63020031110203ec00a18e0230a6cb5817b43524df13f160e092fbc7ab04f57f、jobs SHA6feceb90bc37230cddeeac3d9a70de3f40bc504f2c4709707244161ed71f4919。既有本機非啟動日誌備份及異地檔案stat皆不變；本輪小型DB與收據另列，原啟動日誌允許GET存取增長。
- 不執行去重、異地遷移、正式資料寫回、模型呼叫、會議重跑或刪除。歷史失敗1筆保留，非本次新增。首次基線及重啟後smoke曾碰到備份非同步刷新，保留pending觀測，待實際完整驗證ok才通過。UI後保全首個受限環境看不到既有異地磁碟；同一唯讀核對在正常主機權限通過，未變更權限／設定或寫異地以規避。

## 正式瀏覽器驗證

- 本輪瀏覽器清單沒有先前Chrome分頁，使用可用IAB同LAN /static/index.html，固定同一tab1於部署前後reload，502×668實際viewport；未為截圖重設尺寸。版型未變，既有三viewport版型證據重用，不宣稱重做全套RWD或真實收音驗收。
- 修復前hint仍是HTML預設「預設上限」；修復後由腳本初始化為「單檔上限20MiB」，兩個src均為/static。讀取68會議、70任務、78原始檔／4.7GiB及備份4.1GiB，維運正常；無可見inline-error／alert、console warn／error或水平overflow。
- 開啟未送出的上傳視窗，鍵盤可到達前次Word欄位，提示及未選檔狀態正確，開始處理在無主檔時disabled；僅關閉，不選檔／上傳。唯讀開第68份詳情確認「補前次重產」入口存在且enabled，不點擊重產，不建立任務。保留before、before-upload、after-reload（初始loading，非最終成功）、after-upload-field及after-detail截圖與before／after.json。

## Git與證據收尾

- 已驗證的產品1e871ef已push兩個既有feature分支，正式產品保持此提交；後續僅提交推送本歸檔及兩份任務入口，不為發版文件再次部署或重啟。最終各分支與各自遠端SHA／乾淨狀態以git-sync.json及當次交付核對為準，不要求開發發版文件與正式產品提交相同。
- 正式backups/release_20261022_previous_js/保存baseline、online DB／.env.before、deployment-plan、startup lease取得／釋放、stopped-processes、啟動日誌、所有失敗／通過測試log、pending觀測、preservation-after、smoke與ui-preservation。UI證據在開發tmp/previous-js-ui-20261022/，操作helper及私人收據皆ignored，不推送影音、會議、設定或私有log。
