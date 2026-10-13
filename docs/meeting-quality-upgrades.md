# 會議品質升級

入口：歷史記錄右上角「會議知識庫」。這批程式碼需經正式發版才能出現在正式站；模型及桌面 ZIP 不跟隨 Git 自動部署。

## 收音

- 保留既有瀏覽器錄音／錄影。Chrome 的分享音訊開關由瀏覽器控制，網站無法強制所有選項預設勾選。
- 分享未包含音軌時不開始錄製；取得系統音軌後另行監看音量。安靜 30 秒會提醒，不能把「存在音軌」當成「已錄到每個人的聲音」。
- Windows 桌面錄音不需專用 Chrome，直接取得所選播放裝置的 WASAPI loopback，另存自己的麥克風。這是收音程式，不是新的雲端服務。
- 完成後手動上傳 `meeting.flac`；原始 `system.wav`、`microphone.wav`、開始時間差與錯誤記錄仍在本機。未收音／裝置失敗不顯示成功，合併中斷不發布未完成檔案。
- 裝置切換後需重開並重新選擇。尚須在實際會議用的耳機／喇叭做一次收音驗收；虛擬音訊測試不等於實機通過。

管理員在 Windows x64 / Python 3.14 建置一次，同仁只需解壓縮 ZIP 並執行 EXE，不需 Python：

```powershell
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --require-hashes --no-deps -r requirements-local.lock
.\.venv\Scripts\python.exe -m pip install PyInstaller==6.22.3
.\.venv\Scripts\python.exe scripts/build_desktop_recorder.py
```

產物：`dist/MeetingRecorder-Windows.zip`。部署此檔後，已登入的使用者可從知識庫下載。未建置會明確回應 409。EXE 尚未做程式碼簽章，不應繞過組織的執行管制；大量分發前由 IT 驗證／簽章。程式與依賴原始碼授權在 ZIP 內保留，模型不包進桌面程式。

## 本地發言人與機密模式

伺服器另外安裝模型（首次需要連網下載公開模型，推論期間不連線）：

```powershell
.\.venv\Scripts\python.exe scripts/setup_local_models.py
```

安裝會核對固定 SHA-256，拒絕覆寫既有檔案。若先前安裝中斷，請指定新的空目錄 `--directory C:\MeetingModels-v2`，檢查完成後才更新 `MEETING_LOCAL_MODELS`；不要刪除正在使用的模型。

- 分群：pyannote segmentation + 3D-Speaker embedding，全場一次分群，再對齊逐字稿時間；匿名 A/B 不代表真實姓名，也不跨場記憶聲紋。只保存匿名時間線，不保存聲紋向量。
- 同一逐字稿行包含多位聲音或重疊時顯示「多人／切換待確認」。未辨識出的聲音不推定姓名。缺模型／推論失敗會附上降級原因。
- 目前本地 ASR 是 CPU Whisper base int8，中英文保留原語言（中文轉繁體），時間為 25 秒分段起點，並非逐字時間軸。速度、英文／口音與專業詞準確度須另驗收，不承諾優於雲端模型。
- 「一般」沿用免費雲端模型與既有配額接續；「機密」只產生本地逐字稿草稿，議題／決議／待辦須人工整理，不會以臆測摘要冒充正式紀錄。
- `MEETING_ALLOW_CLOUD=0` 在部署層禁止模型外送；機密音檔 SHA-256 登記後，即使同一檔案重傳選一般仍轉本地。重跑與機密來源詞彙也遵守此規則。
- 機密會議禁止雲端語意檢核、補充資料 AI 判讀、雲端問答；可人工編輯、版本修訂、本地品質檢查。靜態解析器隨站台供應，完整隔離網環境仍需另做部署驗收。
- 機密模式不是端到端加密、逐筆 ACL 或自動保留期限功能。既有角色權限與備份政策照常適用；本機資料夾仍需由 IT 管理讀取權限。不同角色的機密閱讀分權需另設計。

模型來源：[sherpa-onnx 分群模型](https://k2-fsa.github.io/sherpa/onnx/speaker-diarization/models.html)、[Whisper 模型](https://k2-fsa.github.io/sherpa/onnx/pretrained_models/whisper/index.html)。上游授權：[Whisper MIT](https://github.com/openai/whisper/blob/main/LICENSE)、[3D-Speaker Apache-2.0](https://github.com/modelscope/3D-Speaker/blob/main/LICENSE)，segmentation 授權隨壓縮檔保存。安裝時另保存來源授權，不採用含非商業限制的 Reverb 模型。

## 共用詞彙與來源查詢

- Editor 可提出詞彙與誤聽對照，Admin 核准／停用；Viewer 只能讀取。未啟用身份驗證時，沿用既有不分角色模式。
- 僅使用核准詞彙；修正是完全匹配的替換，不是上下文推理。需依語境判斷的同音詞不宜直接列入誤聽對照。人工核准拼法優先於既有硬編碼修正。
- 詞庫在入列時保存快照及雜湊。重試不受後來的詞庫更新影響；既有紀錄不批次重寫。來源會議為機密時該詞彙僅供本地處理。
- 跨會議查詢預設本地關鍵字檢索，不消耗模型額度。最多讀取 200 筆候選會議，回傳 12 段原文，每場最多 4 段；找不到時明確顯示不足，可縮小會議範圍。
- 引用包含會議、時間、目前檔案 SHA-256 與審查狀態。限定已核准時，也核對檔案與核准版本雜湊一致。點選開啟原會議，可在保留媒體存在時跳到對應時間。
- 可另勾選 AI 挑選相關原文；只接受來源中的逐字引文，不採用無法核對的自由生成答案。所有模型額度暫缺時，仍回傳本地原文供工作繼續，不編造答案。
- 資料表 `company_vocabulary`、`vocabulary_events`、`private_sources` 延遲建立。沿用既有 SQLite 備份；此版新增欄位不需要重寫舊會議檔案。

## 驗收界線

離線基準指令：`python scripts/evaluate_speech.py benchmarks/meeting_quality/speech_cases.json`。目前庫內是演算法合成案例，不是實際錄音辨識準確率；見該資料夾 README。

正式上線前另確認：實際同仁耳機雙來源錄音、中文／英文／混語及專業名詞人工對照、長會議 CPU 與記憶體、機密完整離線環境、IT 分發簽章。安裝及功能測試通過不代表這些品質項目已通過。
