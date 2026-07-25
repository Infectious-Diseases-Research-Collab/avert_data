# Download new data, merge it into per-country CSVs, and upload to Supabase --
# in that order, stopping immediately if any step fails so a broken step
# never lets a later one run against stale/partial data.
#
# One-time setup: copy supabase.env.example to supabase.env and fill in your
# real Supabase URL and service-role key (from Supabase -> Project Settings
# -> API). supabase.env is gitignored, so it never gets committed.
#   .\run_full_pipeline.ps1

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# Every run gets its own timestamped transcript, since Task Scheduler invokes
# powershell.exe directly (no shell in between), so ">> file 2>&1" in a
# scheduled task's Action arguments is never interpreted as redirection --
# it's just passed through as unused positional args to this script.
$logDir = Join-Path $PSScriptRoot "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir "pipeline_$(Get-Date -Format 'yyyyMMdd_HHmmss').log"
Start-Transcript -Path $logFile | Out-Null

try {
    if (-not (Test-Path "supabase.env") -and (-not $env:SUPABASE_URL -or -not $env:SUPABASE_SERVICE_ROLE_KEY)) {
        Write-Error "Missing Supabase credentials.`nCopy supabase.env.example to supabase.env and fill in your real values,`nor set `$env:SUPABASE_URL and `$env:SUPABASE_SERVICE_ROLE_KEY yourself first."
        exit 1
    }

    $activate = "venv\Scripts\Activate.ps1"
    if (-not (Test-Path $activate)) {
        Write-Error "Could not find $activate"
        exit 1
    }
    & $activate

    Write-Host "=== 1/3: downloading new data ==="
    python download_data.py
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Write-Host "=== 2/3: merging into CSVs ==="
    python process_data.py
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Write-Host "=== 3/3: uploading to Supabase ==="
    python upload_to_supabase.py
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Write-Host "Done."
} finally {
    Stop-Transcript | Out-Null
}
