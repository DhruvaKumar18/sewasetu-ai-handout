# Sewa Setu - Local Runner Script
Write-Host "Starting Sewa Setu services locally..." -ForegroundColor Cyan

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# 1. Start PostgreSQL (Port 5433)
$pgData = Join-Path $scriptDir "pgdata_local"
$pgBin = "C:\Program Files\PostgreSQL\18\bin"
if (Test-Path $pgData) {
    Write-Host "Starting PostgreSQL on port 5433..." -ForegroundColor Yellow
    & "$pgBin\pg_ctl.exe" -D $pgData -o "-p 5433" -l (Join-Path $pgData "server.log") start
}

# 2. Set environment variables
$env:DB_HOST = "127.0.0.1"
$env:DB_PORT = "5433"
$env:DB_USER = "sewasetu"
$env:DB_PASSWORD = "help"
$env:DB_NAME = "sewasetu"
$env:SMS_GATEWAY_URL = "http://127.0.0.1:8025"
$env:UPLOAD_DIR = Join-Path $scriptDir "uploads"
$env:PUBLIC_BASE_URL = "http://localhost:8000"
$env:ADMIN_USERNAME = "admin"
$env:ADMIN_PASSWORD = if ($env:ADMIN_PASSWORD) { $env:ADMIN_PASSWORD } else { "admin123" }
$env:ADMIN_BEARER_TOKEN = if ($env:ADMIN_BEARER_TOKEN) { $env:ADMIN_BEARER_TOKEN } else { "sewasetu-admin-token-2026" }
$env:CITIZEN_BEARER_TOKEN = if ($env:CITIZEN_BEARER_TOKEN) { $env:CITIZEN_BEARER_TOKEN } else { "sewasetu-citizen-token-2026" }

# 3. Start SMS Gateway if not running
$smsgwRunning = Get-NetTCPConnection -LocalPort 8025 -ErrorAction SilentlyContinue
if (-not $smsgwRunning) {
    Write-Host "Starting SMS Gateway on port 8025..." -ForegroundColor Yellow
    Start-Process python -ArgumentList "smsgw/app.py" -WorkingDirectory $scriptDir -WindowStyle Hidden
    Start-Sleep -Seconds 2
}

Write-Host "`nAll background services are ready!" -ForegroundColor Green
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "  Portal URL:            http://localhost:8000" -ForegroundColor White
Write-Host "  Admin Dashboard:       http://localhost:8000/admin (user: admin / pass: $env:ADMIN_PASSWORD)" -ForegroundColor White
Write-Host "  Citizen MCP (HTTP):    http://localhost:8000/mcp/citizen" -ForegroundColor Green
Write-Host "  Citizen MCP (SSE):     http://localhost:8000/mcp/citizen/sse" -ForegroundColor Green
Write-Host "  Admin MCP (HTTP):      http://localhost:8000/mcp/admin" -ForegroundColor Yellow
Write-Host "  Admin MCP (SSE):       http://localhost:8000/mcp/admin/sse" -ForegroundColor Yellow
Write-Host "  Admin Bearer Token:    $env:ADMIN_BEARER_TOKEN" -ForegroundColor White
Write-Host "  SMS Gateway Console:   http://localhost:8000/__gateway/" -ForegroundColor White
Write-Host "==========================================================`n" -ForegroundColor Cyan

# 4. Run main app
Write-Host "Starting Sewa Setu Portal on http://localhost:8000..." -ForegroundColor Cyan
python app/app.py
