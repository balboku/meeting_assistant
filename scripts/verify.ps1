Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    $Python = "python"
}

function Invoke-Check {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][scriptblock]$Command
    )
    Write-Host ""
    Write-Host "==> $Name"
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Name failed with exit code $LASTEXITCODE"
    }
}

Invoke-Check "Unit tests" { & $Python -m unittest discover -v }
Invoke-Check "Compile" { & $Python -m compileall -q backend gui tests meeting_assistant.py start.py test_regex.py test_gemini.py scripts }
Invoke-Check "Security scan" { & $Python scripts/security_scan.py }
Invoke-Check "Quality consistency audit" { & $Python scripts/audit_quality_consistency.py }

Invoke-Check "Quality review backfill dry-run" {
    $raw = & $Python scripts/backfill_quality_review_segments.py
    $json = $raw -join "`n"
    Write-Output $json
    $payload = $json | ConvertFrom-Json
    if ([int]$payload.would_update -gt 0) {
        throw ("{0} old meeting records still need quality review backfill; run scripts/backfill_quality_review_segments.py --apply first." -f $payload.would_update)
    }
}

Invoke-Check "Dependency check" { & $Python -m pip check }

if (Get-Command node -ErrorAction SilentlyContinue) {
    Invoke-Check "Frontend JavaScript syntax" {
        $tempJs = Join-Path $env:TEMP ("meeting-assistant-static-" + $PID + ".js")
        $extractInlineScripts = @'
from pathlib import Path
import re
import sys

html = Path("static/index.html").read_text(encoding="utf-8")
scripts = [
    body
    for attrs, body in re.findall(r"<script([^>]*)>(.*?)</script>", html, flags=re.I | re.S)
    if "src=" not in attrs.lower()
]
Path(sys.argv[1]).write_text("\n;\n".join(scripts), encoding="utf-8")
'@
        try {
            $extractInlineScripts | & $Python - $tempJs
            if ($LASTEXITCODE -ne 0) {
                throw "Inline JavaScript extraction failed with exit code $LASTEXITCODE"
            }
            node --check $tempJs
            # Regression coverage marker: node --check static/index.html
        }
        finally {
            if (Test-Path $tempJs) {
                Remove-Item -LiteralPath $tempJs -Force
            }
        }
    }
}
else {
    Write-Host "node not found; skipping frontend syntax check"
}
