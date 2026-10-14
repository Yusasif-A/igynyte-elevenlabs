# Start the fraud agent: public tunnel -> .env -> ElevenLabs config -> server.
#   powershell -ExecutionPolicy Bypass -File .\start.ps1           # local + tunnel links
#   powershell -ExecutionPolicy Bypass -File .\start.ps1 -Deploy   # also publish https://noba-bank.web.app
param([int]$Port = 8080, [switch]$SkipSetup, [switch]$Deploy)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    Write-Host "Port $Port is already in use. Stop the other server first (or pass -Port 8090)." -ForegroundColor Red
    exit 1
}

$cloudflared = (Get-Command cloudflared -ErrorAction SilentlyContinue).Source
if (-not $cloudflared) { $cloudflared = "C:\Program Files (x86)\cloudflared\cloudflared.exe" }
if (-not (Test-Path $cloudflared)) {
    Write-Host "cloudflared not found. Install it: winget install Cloudflare.cloudflared" -ForegroundColor Red
    exit 1
}

$log = Join-Path $env:TEMP "fraud-agent-tunnel.log"
$tunnel = $null

try {
    # Cloudflare's free quick-tunnel service sometimes times out; retry. Only a
    # random-word subdomain is a tunnel -- api.trycloudflare.com appears in the
    # error line when the request fails.
    $url = $null
    for ($attempt = 1; $attempt -le 3 -and -not $url; $attempt++) {
        Write-Host "Opening tunnel (attempt $attempt)..." -NoNewline
        Remove-Item $log -ErrorAction SilentlyContinue
        $tunnel = Start-Process $cloudflared -ArgumentList "tunnel", "--no-autoupdate", "--url", "http://localhost:$Port" `
            -RedirectStandardError $log -WindowStyle Hidden -PassThru
        for ($i = 0; $i -lt 60 -and -not $url; $i++) {
            Start-Sleep -Milliseconds 500
            $m = Select-String -Path $log -Pattern 'https://(?!api\.)[a-z0-9]+(-[a-z0-9]+)+\.trycloudflare\.com' -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($m) { $url = $m.Matches[0].Value; break }
            if ($tunnel.HasExited -or (Select-String -Path $log -Pattern 'failed to request quick Tunnel' -Quiet -ErrorAction SilentlyContinue)) { break }
        }
        if (-not $url) {
            Write-Host " failed" -ForegroundColor Yellow
            if (-not $tunnel.HasExited) { Stop-Process -Id $tunnel.Id -Force }
            Start-Sleep -Seconds 3
        }
    }
    if (-not $url) { throw "Cloudflare did not give a tunnel after 3 tries (see $log). Try again in a minute." }
    Write-Host " $url" -ForegroundColor Green
    # Give the new hostname a moment to start resolving before ElevenLabs uses it.
    Start-Sleep -Seconds 3

    # Write EL_TOOLS_BASE_URL into .env, replacing any previous value.
    $envLines = @(Get-Content .env -Encoding utf8 | Where-Object { $_ -notmatch '^\s*EL_TOOLS_BASE_URL\s*=' })
    $envLines += "EL_TOOLS_BASE_URL=$url"
    Set-Content .env $envLines -Encoding utf8

    if (-not $SkipSetup) {
        Write-Host "Pointing the ElevenLabs agent at the new URL (el_setup.py)..."
        $env:PYTHONIOENCODING = "utf-8"
        python el_setup.py | Select-String -Pattern "agent  |llm  |voices|tools  |knowledge|telephony|Traceback|Error"
        if ($LASTEXITCODE -ne 0) { throw "el_setup.py failed" }
    }

    if ($Deploy) {
        # The hosted pages call this run's tunnel, so they are rebuilt and
        # redeployed with its URL every time.
        Write-Host "Publishing the pages to Firebase (noba-bank)..."
        python build_web.py --api $url
        if ($LASTEXITCODE -ne 0) { throw "build_web.py failed" }
        Push-Location web
        try {
            firebase deploy --only hosting --project noba-bank --non-interactive | Select-String -Pattern "Hosting URL|Error|error"
            if ($LASTEXITCODE -ne 0) { throw "firebase deploy failed" }
        } finally { Pop-Location }
        Write-Host ""
        Write-Host "  Share:  https://noba-bank.web.app       (checkout)" -ForegroundColor Green
        Write-Host "          https://noba-bank.web.app/ui    (bank console)" -ForegroundColor Green
    }

    Write-Host ""
    Write-Host "  Checkout (customer):  $url/checkout" -ForegroundColor Cyan
    Write-Host "  Fraud console (bank): $url/ui" -ForegroundColor Cyan
    Write-Host "  Local:                http://localhost:$Port/checkout"
    Write-Host "  Ctrl+C to stop."
    Write-Host ""
    python -m uvicorn el_tools:app --host 0.0.0.0 --port $Port
}
finally {
    if ($tunnel -and -not $tunnel.HasExited) { Stop-Process -Id $tunnel.Id -Force }
    Write-Host "Tunnel closed."
}
