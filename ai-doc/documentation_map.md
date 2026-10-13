# 會議品質升級

目前工作：使用者已授權六項品質升級 commit／push／部署，日曆自動化除外。開發副本分支 `feat/meeting-quality-upgrades`，正式分支 `feat/transcript-chinese-translation`，部署前基線 `43d4e38`。

- 工作與驗收契約：[dev_task.md](dev_task.md)
- 六項程式實作完成，最終 706 項全套回歸通過（含備份內容變動偵測）。產品提交 `22e677a` 已推送；收音硬體及實際會議準確率仍待驗收，詳見工作契約的 QC 證據與限制。
- 操作／安裝說明：[meeting-quality-upgrades.md](../docs/meeting-quality-upgrades.md)
- 功能測試使用隔離資料庫與虛構資料；語音整合僅用公開模型樣本，沒有處理公司錄音。真實準確率需要人工標註，不以格式測試代替。
- 本輪發版順序、停止條件、備份、回復與正式環境驗證，集中於 `dev_task.md` 的發版契約；未驗證項目不計為完成。正式資料不重跑、不重寫，原有權限與雲端設定不變。
- 實際切換結果讀取正式主機 `backups/release_20261013_quality/` 下的 `backup-verification.json`、`preservation-check.json`、`smoke.json`，不得只用「已推送」推定「已部署」。下一個品質驗收是實機雙來源收音及真實會議人工標註；瀏覽器工具恢復後補正式站畫面複驗。
