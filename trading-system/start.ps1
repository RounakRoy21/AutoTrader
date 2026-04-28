# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# AutoTrader â€” single launch script (Windows / local dev)
#
# Usage:
#   .\start.ps1           # start all services (no image rebuild)
#   .\start.ps1 -Build    # rebuild images first (use after code changes)
#   .\start.ps1 -Stop     # gracefully stop all services
#   .\start.ps1 -Status   # print container status and exit
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
[CmdletBinding()]
param(
    [switch]$Build,
    [switch]$Stop,
    [switch]$Status
)

$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot

# â”€â”€ Colour helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
function Write-Info { param($msg) Write-Host "[INFO]  $msg" -ForegroundColor Cyan   }
function Write-Ok   { param($msg) Write-Host "[ OK ]  $msg" -ForegroundColor Green  }
function Write-Warn { param($msg) Write-Host "[WARN]  $msg" -ForegroundColor Yellow }
function Write-Err  {
    param($msg)
    Write-Host "[ERR ]  $msg" -ForegroundColor Red
    Pop-Location; exit 1
}
function Write-Banner {
    param($title)
    Write-Host ""
    Write-Host "  ============================================" -ForegroundColor DarkCyan
    Write-Host "  AutoTrader -- $title" -ForegroundColor DarkCyan
    Write-Host "  ============================================" -ForegroundColor DarkCyan
    Write-Host ""
}

# â”€â”€ Pre-checks â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Err "docker not found. Install Docker Desktop."
}

# â”€â”€ --Stop â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
if ($Stop) {
    Write-Banner "Stopping"
    docker-compose down
    Write-Ok "All containers stopped (database volume preserved)."
    Pop-Location; exit 0
}

# â”€â”€ --Status â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
if ($Status) {
    docker-compose ps
    Pop-Location; exit 0
}

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
Write-Banner "Starting (local dev)"

# â”€â”€ 1. Pre-flight checks â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
Write-Info "Running pre-flight checks..."

if (-not (Test-Path ".env")) {
    Write-Err ".env not found. Copy .env.example to .env and fill in your secrets."
}
$envContent = Get-Content ".env" -Raw
if ($envContent -match '<[^>]+>') {
    Write-Err ".env still contains placeholder values. Fill them all in then rerun."
}
Write-Ok ".env looks populated"

$ErrorActionPreference = "Continue"
$null = docker info 2>&1
$ErrorActionPreference = "Stop"
if ($LASTEXITCODE -ne 0) {
    Write-Err "Docker daemon is not running. Start Docker Desktop first."
}
Write-Ok "Docker is running"

# â”€â”€ 2. Start containers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# depends_on: condition: service_healthy ensures postgres and redis are ready
# before the backend starts â€” no manual health-wait loops needed.
Write-Info "Starting containers$(if ($Build) { ' (with image rebuild)' })..."
if ($Build) {
    docker-compose up -d --build
} else {
    docker-compose up -d
}
Write-Host ""

# â”€â”€ 3. Wait for backend API â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
Write-Info "Waiting for backend API (up to 90 s)..."
$backendUp = $false
for ($i = 1; $i -le 45; $i++) {
    try {
        $resp = Invoke-WebRequest -Uri "http://localhost:8000/api/health" -UseBasicParsing -TimeoutSec 2 -ErrorAction SilentlyContinue
        if ($resp.StatusCode -eq 200) { Write-Ok "backend API is up"; $backendUp = $true; break }
    } catch { }
    Start-Sleep -Seconds 2
}
if (-not $backendUp) {
    Write-Warn "Backend health check timed out. Migrations will still be attempted. Run: docker compose logs backend"
}

# â”€â”€ 4. Database migrations â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
Write-Info "Applying database migrations..."
docker exec autotrader-backend alembic upgrade head
Write-Ok "Migrations applied"

# â”€â”€ 5. Summary â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
$envLines = Get-Content ".env" | Where-Object { $_ -match '^PAPER_TRADING=' }
$paper = if ($envLines) { (($envLines[0] -split '=',2)[1] + '').Trim() } else { "unknown" }
if (-not $paper) { $paper = "unknown" }

Write-Host ""
Write-Host "  ============================================" -ForegroundColor Green
Write-Host "  AutoTrader is running" -ForegroundColor Green
Write-Host "  ============================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Mode:       $paper"                                     -ForegroundColor White
Write-Host "  Dashboard:  http://localhost:4201"                      -ForegroundColor White
Write-Host "  API health: http://localhost:8000/api/health"           -ForegroundColor White
Write-Host "  API docs:   http://localhost:8000/docs"                 -ForegroundColor White
Write-Host ""
Write-Host "  Groww auth (one-time): POST http://localhost:8000/api/auth/groww/login" -ForegroundColor Yellow
Write-Host "  (only needed once - TOTP tokens do not expire)"         -ForegroundColor Yellow
Write-Host ""
Write-Host "  Useful commands:"                                        -ForegroundColor DarkGray
Write-Host "    docker-compose logs -f backend          # live backend logs"   -ForegroundColor DarkGray
    Write-Host "    docker-compose logs -f backend | Select-String scanner"        -ForegroundColor DarkGray
Write-Host "    .\start.ps1 -Stop                       # graceful shutdown"    -ForegroundColor DarkGray
Write-Host ""

docker-compose ps

Pop-Location
