# Download new data, merge it into per-country CSVs, and upload to Supabase --
# in that order, stopping immediately if any step fails so a broken step
# never lets a later one run against stale/partial data.
#
# One-time setup: copy supabase.env.example to supabase.env and fill in your
# real Supabase URL and service-role key (from Supabase -> Project Settings
# -> API). supabase.env is gitignored, so it never gets committed.
#
# Optional: copy smtp.json.example to smtp.json and fill in real SMTP
# credentials to get an email on failure. If smtp.json is absent, failures
# are still logged, just not emailed.
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

function Send-FailureEmail {
    param([string]$Message)

    $smtpConfigFile = Join-Path $PSScriptRoot "smtp.json"
    if (-not (Test-Path $smtpConfigFile)) {
        Write-Warning "smtp.json not found -- skipping failure email. Copy smtp.json.example to smtp.json to enable it."
        return
    }

    try {
        $cfg = Get-Content $smtpConfigFile -Raw | ConvertFrom-Json
        $securePassword = ConvertTo-SecureString $cfg.password -AsPlainText -Force
        $cred = New-Object System.Management.Automation.PSCredential($cfg.username, $securePassword)
        $body = "The AVERT data pipeline failed on $(Get-Date).`n`n$Message`n`nFull log: $logFile"
        $useSsl = [bool]$cfg.use_ssl
        Send-MailMessage -From $cfg.from -To $cfg.to `
            -Subject "AVERT pipeline FAILED - $(Get-Date -Format 'yyyy-MM-dd HH:mm')" `
            -Body $body -SmtpServer $cfg.smtp_server -Port $cfg.smtp_port `
            -UseSsl:$useSsl -Credential $cred
    } catch {
        Write-Warning "Failed to send failure email: $_"
    }
}

$failureExitCode = 1
try {
    if (-not (Test-Path "supabase.env") -and (-not $env:SUPABASE_URL -or -not $env:SUPABASE_SERVICE_ROLE_KEY)) {
        throw "Missing Supabase credentials.`nCopy supabase.env.example to supabase.env and fill in your real values,`nor set `$env:SUPABASE_URL and `$env:SUPABASE_SERVICE_ROLE_KEY yourself first."
    }

    $activate = "venv\Scripts\Activate.ps1"
    if (-not (Test-Path $activate)) {
        throw "Could not find $activate"
    }
    & $activate

    Write-Host "=== 1/3: downloading new data ==="
    python download_data.py
    if ($LASTEXITCODE -ne 0) { $failureExitCode = $LASTEXITCODE; throw "download_data.py exited with code $LASTEXITCODE" }

    Write-Host "=== 2/3: merging into CSVs ==="
    python process_data.py
    if ($LASTEXITCODE -ne 0) { $failureExitCode = $LASTEXITCODE; throw "process_data.py exited with code $LASTEXITCODE" }

    Write-Host "=== 3/3: uploading to Supabase ==="
    python upload_to_supabase.py
    if ($LASTEXITCODE -ne 0) { $failureExitCode = $LASTEXITCODE; throw "upload_to_supabase.py exited with code $LASTEXITCODE" }

    Write-Host "Done."
} catch {
    Write-Host "PIPELINE FAILED: $_"
    Send-FailureEmail -Message $_.ToString()
    exit $failureExitCode
} finally {
    Stop-Transcript | Out-Null
}
