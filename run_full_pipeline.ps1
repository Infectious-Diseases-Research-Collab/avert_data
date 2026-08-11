# Download new data, merge it into per-country CSVs, and upload to Supabase --
# in that order, stopping immediately if any step fails so a broken step
# never lets a later one run against stale/partial data.
#
# One-time setup: copy supabase.env.example to supabase.env and fill in your
# real Supabase URL and service-role key (from Supabase -> Project Settings
# -> API). supabase.env is gitignored, so it never gets committed.
#
# Optional: copy smtp.json.example to smtp.json and fill in real SMTP
# credentials to get an email when a run fails, and when a run succeeds but
# raises warnings that need someone to look at them. If smtp.json is absent,
# both are still logged, just not emailed.
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

function Send-PipelineEmail {
    param([string]$Subject, [string]$Body)

    $smtpConfigFile = Join-Path $PSScriptRoot "smtp.json"
    if (-not (Test-Path $smtpConfigFile)) {
        Write-Warning "smtp.json not found -- skipping email. Copy smtp.json.example to smtp.json to enable it."
        return
    }

    try {
        $cfg = Get-Content $smtpConfigFile -Raw | ConvertFrom-Json
        $securePassword = ConvertTo-SecureString $cfg.password -AsPlainText -Force
        $cred = New-Object System.Management.Automation.PSCredential($cfg.username, $securePassword)
        $useSsl = [bool]$cfg.use_ssl
        Send-MailMessage -From $cfg.from -To $cfg.to `
            -Subject $Subject `
            -Body "$Body`n`nFull log: $logFile" `
            -SmtpServer $cfg.smtp_server -Port $cfg.smtp_port `
            -UseSsl:$useSsl -Credential $cred
    } catch {
        # A mail problem must never turn an otherwise-fine run into a failure.
        Write-Warning "Failed to send email: $_"
    }
}

$failureExitCode = 1
# Warnings mean the data went up but something needs a human. Collected from
# the upload step and emailed after a successful finish, never thrown.
$warningLines = @()
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
    # Tee rather than assign, so the lines still reach the host (and so the
    # transcript) as they happen. $LASTEXITCODE survives the pipeline because
    # Tee-Object is a cmdlet, not a native command.
    #
    # Deliberately NOT "2>&1": merging a native command's stderr into the
    # success stream turns each line into an ErrorRecord, and with
    # $ErrorActionPreference = "Stop" that throws NativeCommandError -- so one
    # stray warning on stderr would be caught below and reported as a failed
    # run. stderr goes straight to the transcript instead, as it always has.
    python upload_to_supabase.py | Tee-Object -Variable uploadOutput
    $uploadExit = $LASTEXITCODE
    if ($uploadExit -eq 3) {
        # Exit 3 = everything uploaded, but something needs a human. Emailed
        # below rather than thrown: this is a run that worked.
        $warningLines = @($uploadOutput | Where-Object { $_ -match '^\s*!\s' })
    } elseif ($uploadExit -ne 0) {
        $failureExitCode = $uploadExit
        throw "upload_to_supabase.py exited with code $uploadExit"
    }

    Write-Host "Done."

    if ($warningLines) {
        Send-PipelineEmail `
            -Subject "AVERT pipeline completed WITH WARNINGS - $(Get-Date -Format 'yyyy-MM-dd HH:mm')" `
            -Body ("The AVERT data pipeline completed on $(Get-Date) and all data was uploaded," +
                   " but it raised warnings that need attention:`n`n" +
                   (($warningLines | ForEach-Object { $_.ToString().Trim() }) -join "`n") +
                   "`n`nThese are also listed in the dashboard's Data quality section.")
    }
} catch {
    Write-Host "PIPELINE FAILED: $_"
    Send-PipelineEmail `
        -Subject "AVERT pipeline FAILED - $(Get-Date -Format 'yyyy-MM-dd HH:mm')" `
        -Body "The AVERT data pipeline failed on $(Get-Date).`n`n$_"
    exit $failureExitCode
} finally {
    Stop-Transcript | Out-Null
}
