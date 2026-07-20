param(
    [string]$BaseUrl = $env:BASE_URL
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Net.Http

if ([string]::IsNullOrWhiteSpace($BaseUrl)) {
    $BaseUrl = "http://127.0.0.1:8001"
}
$BaseUrl = $BaseUrl.TrimEnd("/")

$TempDir = Join-Path ([System.IO.Path]::GetTempPath()) ("meeting-assistant-smoke-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $TempDir | Out-Null

function Invoke-SmokeGet {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$OutFile
    )
    $uri = if ($Path.StartsWith("http")) { $Path } else { "$BaseUrl$Path" }
    Invoke-WebRequest -Uri $uri -OutFile $OutFile -MaximumRedirection 5 -UseBasicParsing | Out-Null
}

function Assert-FileContains {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Text
    )
    $content = Get-Content -Path $Path -Raw -Encoding UTF8
    if (-not $content.Contains($Text)) {
        throw "Expected $Path to contain: $Text"
    }
}

try {
    Invoke-SmokeGet "/health" (Join-Path $TempDir "health.json")
    Invoke-SmokeGet "/metrics" (Join-Path $TempDir "metrics.json")
    $historyPath = Join-Path $TempDir "history.html"
    Invoke-SmokeGet "/history" $historyPath
    Invoke-SmokeGet "/static/vendor/marked.min.js" (Join-Path $TempDir "marked.min.js")
    Invoke-SmokeGet "/static/vendor/purify.min.js" (Join-Path $TempDir "purify.min.js")

    Assert-FileContains $historyPath 'id="ops-dashboard"'
    Assert-FileContains $historyPath 'id="job-dashboard"'

    $client = [System.Net.Http.HttpClient]::new()
    $multipart = [System.Net.Http.MultipartFormDataContent]::new()
    $fileContent = [System.Net.Http.ByteArrayContent]::new([System.Text.Encoding]::UTF8.GetBytes("<html>not audio</html>"))
    $fileContent.Headers.ContentType = [System.Net.Http.Headers.MediaTypeHeaderValue]::Parse("audio/mpeg")
    $multipart.Add($fileContent, "file", "fake.mp3")

    try {
        $response = $client.PostAsync("$BaseUrl/upload-media", $multipart).GetAwaiter().GetResult()
        $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        Set-Content -Path (Join-Path $TempDir "fake-upload.json") -Value $body -Encoding UTF8
        $statusCode = [int]$response.StatusCode
        if ($statusCode -ne 415) {
            throw "expected fake.mp3 upload to return 415, got $statusCode`n$body"
        }
    }
    finally {
        $multipart.Dispose()
        $fileContent.Dispose()
        $client.Dispose()
    }

    Write-Host "smoke ok: $BaseUrl"
}
finally {
    if (Test-Path $TempDir) {
        Remove-Item -LiteralPath $TempDir -Recurse -Force
    }
}
