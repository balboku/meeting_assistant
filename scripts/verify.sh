#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

.venv/bin/python -m unittest discover -v
.venv/bin/python -m compileall -q backend gui tests meeting_assistant.py start.py test_regex.py test_gemini.py
.venv/bin/python scripts/security_scan.py
.venv/bin/python scripts/audit_quality_consistency.py
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
