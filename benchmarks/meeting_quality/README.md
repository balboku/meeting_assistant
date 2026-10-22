# Meeting Quality Benchmark

這個資料夾用來放「已人工確認」的會議紀錄品質基準。基準測試是離線的，不會呼叫 Gemini/Gemma，也不會產生 API 費用。

## 使用方式

1. 先挑一份人工確認過的 Markdown 會議紀錄。
2. 若內容含個資、客戶、供應商或內部機密，先建立匿名化版本。
3. 在 manifest JSON 新增案例，填入 `markdown_path` 與期望條件。
4. 執行：

```bash
.venv/bin/python scripts/run_quality_benchmark.py benchmarks/meeting_quality/cases.example.json --min-score 80
```

也可以直接掃描目前產出的 Markdown 會議紀錄，先抓出需要人工複核的低分檔案：

```bash
.venv/bin/python scripts/run_quality_benchmark.py --scan-dir output --limit 20 --min-score 75 --format summary
```

若要用較保守的文字密度門檻，找出長分段有時間戳但內容異常稀疏的紀錄，可加入：

```bash
.venv/bin/python scripts/run_quality_benchmark.py --scan-dir output --limit 20 --min-score 75 --min-segment-chars-per-second 1.5 --segment-density-min-duration-seconds 120 --format summary
```

## 可檢查項目

- 四大區塊是否存在：討論摘要、最終決議、待辦事項、完整逐字稿。
- 討論摘要是否使用 `D1`, `D2` 等編號。
- 決議與待辦是否能連回既有 `D` / `R` 編號。
- 逐字稿是否保留時間戳與分段標題。
- 逐字稿是否出現「為節省篇幅」「已省略逐字稿」等省略提示。
- 逐字稿是否有同一句話連續重複形成循環。
- 可選擇檢查長分段的逐字稿文字量/時間比，抓出格式完整但內容被過度濃縮的輸出。
- 必要術語是否存在，例如 `佳世達`, `IEC 62304`。
- 禁用誤聽詞是否不存在，例如 `加斯達`, `IEC 6304`。

這個工具適合在修改 prompt、模型或摘要格式後使用。它不能取代人工審查，但可以擋掉常見退步。

## 語音文字與發言人評分

```powershell
.\.venv\Scripts\python.exe scripts/evaluate_speech.py benchmarks/meeting_quality/speech_cases.json
```

新增工具比較分開的 reference / prediction JSON，輸出中文 CER、英文 WER、混語 token error rate、術語召回率、數字序列錯誤率，以及包含重疊語音、零 collar 的 DER。匿名 A/B 會先做整場最佳配對，不因代號互換扣分；漏人、多人被合併或誤加人會扣分。門檻失敗會回傳非零結束碼。

目前 `speech_reference.json` 明確標為 `synthetic_algorithm_fixture_not_audio_accuracy`，只檢查演算法，不能拿零分誤差當作實際辨識率。

實測請另建不提交 Git 的資料集：至少中文、英文、混語、專業名詞、遠端收音、重疊發言各一段。人工聽寫原文及 speaker start/end（秒），填 `human_verified: true`、原錄音 `audio_sha256`；預測必須由待驗模型獨立產生。評分結果同時保存 reference / prediction SHA-256，才能比較同一資料與模型版本。不得用同一份 AI 文字同時當標準答案與預測。

離線評分輸入會先驗證：

- manifest 必須是非空案例陣列，名稱不重複；每案至少有一項 `maximum` 或 `minimum` 門檻。未知指標、非有限／負門檻及互相矛盾的上下限不接受；錯誤率可超過 1，術語召回率限 0–1。
- 真實標準答案的 `human_verified` 必須是 JSON 布林值 `true`，字串 `"true"`／`"false"` 都不算。reference 與 prediction 須為不同檔案，兩邊均填同一原錄音的 64 位十六進位 `audio_sha256`。工具只核對宣告，不讀取音檔，也無法證明文字確由人工聽寫；匿名化或剪輯後應使用實際比較音檔的 SHA-256，不能混用兩段錄音。
- 標準答案必須有可評分的中英文文字；指定中文／英文／數字指標時，也必須有對應文字，不能把無資料當零錯誤。空白 prediction 仍可評分，會計入漏字。設定 `der` 門檻時，reference 必須有非空發言人時間線；區間需為有限數字、`0 <= start < end`，匿名代號不可空白。漏掉 prediction 的發言人會計為漏辨，不冒充有效標註。
- 結束碼 `0` 是指定門檻通過，`1` 是有評分但門檻未過，`2` 是輸入無效／檔案不可讀；後兩者 JSON 均回傳 `passed: false`。

報告另列 `human_reference_case_count`、`synthetic_case_count` 與 `evaluation_scope`。庫內資料輸出 `synthetic_algorithm_only`，即使 `passed: true` 也只表示算法通過，不能宣稱公司錄音準確率已達標；虛構的測試資料亦不能視為真實錄音驗收證據。
