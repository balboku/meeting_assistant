param(
    [int]$Port = 8001,
    [string]$HostAddress = "127.0.0.1",
    [string]$BaseUrl = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

if ([string]::IsNullOrWhiteSpace($BaseUrl)) {
    $BaseUrl = "http://{0}:{1}" -f $HostAddress, $Port
}
$BaseUrl = $BaseUrl.TrimEnd("/")

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    $Python = "python"
}

$LogsDir = Join-Path $RepoRoot "logs"
New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null

function Test-SmokeServerReady {
    param([Parameter(Mandatory = $true)][string]$Url)
    try {
        Invoke-WebRequest -Uri "$Url/health" -TimeoutSec 2 -UseBasicParsing | Out-Null
        return $true
    }
    catch {
        return $false
    }
}

function Invoke-SmokeScript {
    param([Parameter(Mandatory = $true)][string]$Url)
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "smoke_e2e.ps1") -BaseUrl $Url
    if ($LASTEXITCODE -ne 0) {
        throw "smoke_e2e.ps1 failed with exit code $LASTEXITCODE"
    }
}

if (Test-SmokeServerReady $BaseUrl) {
    Write-Host "Using existing Meeting Assistant server: $BaseUrl"
    Invoke-SmokeScript $BaseUrl
    exit 0
}

$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$OutLog = Join-Path $LogsDir "smoke-with-server-$Timestamp.out.log"
$ErrLog = Join-Path $LogsDir "smoke-with-server-$Timestamp.err.log"
$ServerProcess = $null

try {
    Write-Host "Starting temporary Meeting Assistant server: $BaseUrl"
    $ServerProcess = Start-Process `
        -FilePath $Python `
        -ArgumentList @("-m", "uvicorn", "backend.main:app", "--host", $HostAddress, "--port", [string]$Port) `
        -WorkingDirectory $RepoRoot `
        -PassThru `
        -WindowStyle Hidden `
        -RedirectStandardOutput $OutLog `
        -RedirectStandardError $ErrLog

    $ready = $false
    for ($i = 0; $i -lt 45; $i++) {
        Start-Sleep -Seconds 1
        if ($ServerProcess.HasExited) {
            break
        }
        if (Test-SmokeServerReady $BaseUrl) {
            $ready = $true
            break
        }
    }

    if (-not $ready) {
        Write-Host "Temporary server did not become ready. Recent logs:"
        if (Test-Path $ErrLog) {
            Get-Content -Path $ErrLog -Tail 80
        }
        if (Test-Path $OutLog) {
            Get-Content -Path $OutLog -Tail 80
        }
        throw "Meeting Assistant server did not become ready at $BaseUrl"
    }

    Invoke-SmokeScript $BaseUrl
}
finally {
    if ($ServerProcess -and -not $ServerProcess.HasExited) {
        Stop-Process -Id $ServerProcess.Id -Force
        $ServerProcess.WaitForExit(5000) | Out-Null
        Write-Host "Stopped temporary Meeting Assistant server."
    }
}
