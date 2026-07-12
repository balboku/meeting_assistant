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

## 可檢查項目

- 四大區塊是否存在：討論摘要、最終決議、待辦事項、完整逐字稿。
- 討論摘要是否使用 `D1`, `D2` 等編號。
- 決議與待辦是否能連回既有 `D` / `R` 編號。
- 逐字稿是否保留時間戳與分段標題。
- 逐字稿是否出現「為節省篇幅」「已省略逐字稿」等省略提示。
- 逐字稿是否有同一句話連續重複形成循環。
- 必要術語是否存在，例如 `佳世達`, `IEC 62304`。
- 禁用誤聽詞是否不存在，例如 `加斯達`, `IEC 6304`。

這個工具適合在修改 prompt、模型或摘要格式後使用。它不能取代人工審查，但可以擋掉常見退步。
