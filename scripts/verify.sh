#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

.venv/bin/python -m unittest discover -v
.venv/bin/python -m compileall -q backend gui tests meeting_assistant.py start.py test_regex.py test_gemini.py scripts
.venv/bin/python scripts/security_scan.py
.venv/bin/python scripts/audit_quality_consistency.py
.venv/bin/python - <<'PY'
import json
import subprocess
import sys

result = subprocess.run(
    [sys.executable, "scripts/backfill_quality_review_segments.py"],
    check=True,
    capture_output=True,
    text=True,
    encoding="utf-8",
)
print(result.stdout, end="")
payload = json.loads(result.stdout)
if int(payload.get("would_update") or 0) > 0:
    raise SystemExit(
        "舊紀錄仍有可回填的問題分段；請先執行 "
        ".venv/bin/python scripts/backfill_quality_review_segments.py --apply"
    )
PY
.venv/bin/python -m pip check

if command -v node >/dev/null 2>&1; then
    tmp_js="$(mktemp "${TMPDIR:-/tmp}/meeting-assistant-static-js.XXXXXX.js")"
    trap 'rm -f "$tmp_js"' EXIT
    .venv/bin/python - "$tmp_js" <<'PY'
from pathlib import Path
import re
import sys

target = Path(sys.argv[1])
html = Path("static/index.html").read_text(encoding="utf-8")
inline_scripts = []
for attrs, body in re.findall(r"<script([^>]*)>(.*?)</script>", html, flags=re.I | re.S):
    if "src=" not in attrs.lower():
        inline_scripts.append(body)
target.write_text("\n;\n".join(inline_scripts), encoding="utf-8")
PY
    node --check "$tmp_js"
    # Regression coverage marker: node --check static/index.html
else
    echo "node not found; skipping frontend syntax check"
fi
