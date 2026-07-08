#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8001}"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

curl -fsS "$BASE_URL/health" > "$tmp_dir/health.json"
curl -fsS "$BASE_URL/metrics" > "$tmp_dir/metrics.json"
curl -fsSL "$BASE_URL/history" > "$tmp_dir/history.html"
curl -fsS "$BASE_URL/static/vendor/marked.min.js" > /dev/null
curl -fsS "$BASE_URL/static/vendor/purify.min.js" > /dev/null

grep -q 'id="ops-dashboard"' "$tmp_dir/history.html"
grep -q 'id="job-dashboard"' "$tmp_dir/history.html"

upload_status="$(
    printf '<html>not audio</html>' |
        curl -sS -o "$tmp_dir/fake-upload.json" -w '%{http_code}' \
            -F 'file=@-;filename=fake.mp3;type=audio/mpeg' \
            "$BASE_URL/upload-audio"
)"

if [ "$upload_status" != "415" ]; then
    echo "expected fake.mp3 upload to return 415, got $upload_status" >&2
    cat "$tmp_dir/fake-upload.json" >&2
    exit 1
fi

echo "smoke ok: $BASE_URL"
