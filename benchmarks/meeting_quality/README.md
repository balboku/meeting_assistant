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
