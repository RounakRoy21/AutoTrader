# ──────────────────────────────────────────────────────────────────────────────
# AutoTrader — single launch script (Windows / local dev)
#
# Usage:
#   .\start.ps1           # start all services (no image rebuild)
#   .\start.ps1 -Build    # rebuild images first (use after code changes)
#   .\start.ps1 -Stop     # gracefully stop all services
#   .\start.ps1 -Status   # print container status and exit
# ──────────────────────────────────────────────────────────────────────────────
[CmdletBinding()]
param(
    [switch]$Build,
    [switch]$Stop,
    [switch]$Status
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Push-Location $PSScriptRoot

# ── Colour helpers ─────────────────────────────────────────────────────────────
function Write-Info    { param($msg) Write-Host "[INFO]  $msg" -ForegroundColor Cyan    }
function Write-Ok      { param($msg) Write-Host "[ OK ]  $msg" -ForegroundColor Green   }
function Write-Warn    { param($msg) Write-Host "[WARN]  $msg" -ForegroundColor Yellow  }
function Write-Err     { param($msg) Write-Host "[ERR ]  $msg" -ForegroundColor Red; Pop-Location; exit 1 }

function Write-Banner  {
    param($title)
    Write-Host ""
    Write-Host "  ╔══════════════════════════════════════════╗" -ForegroundColor DarkCyan
    Write-Host "  ║   AutoTrader — $title" -ForegroundColor DarkCyan
    Write-Host "  ╚══════════════════════════════════════════╝" -ForegroundColor DarkCyan
    Write-Host ""
}

# ── --Stop ─────────────────────────────────────────────────────────────────────
if ($Stop) {
    Write-Banner "Stopping"
    docker compose down
    Write-Ok "All containers stopped (database volume preserved)."
    Pop-Location; exit 0
}

# ── --Status ──────────────────────────────────────────────────────────────────
if ($Status) {
    docker compose ps
    Pop-Location; exit 0
}

# ══════════════════════════════════════════════════════════════════════════════
# START
# ══════════════════════════════════════════════════════════════════════════════
Write-Banner "Starting (local dev)"

# ── 1. Pre-flight checks ───────────────────────────────────────────────────────
Write-Info "Running pre-flight checks..."

if (-not (Test-Path ".env")) {
    Write-Err ".env not found.`n       Copy .env.example -> .env and fill in your secrets, then rerun."
}

$envContent = Get-Content ".env" -Raw
if ($envContent -match '<[^>]+>') {
    Write-Err ".env still contains placeholder values (e.g. <your API Key>).`n       Fill them all in, then rerun."
}
Write-Ok ".env looks populated"

if (-not (Get-Command "docker" -ErrorAction SilentlyContinue)) {
    Write-Err "Docker is not installed or not in PATH. Install Docker Desktop first."
}
try { docker info 2>&1 | Out-Null } catch {
    Write-Err "Docker daemon is not running. Start Docker Desktop first."
}
Write-Ok "Docker is running"

# ── 2. Start containers ────────────────────────────────────────────────────────
$buildArg = if ($Build) { "--build" } else { "" }
Write-Info "Starting containers$(if ($Build) { ' (with image rebuild)' })..."
if ($Build) {
    docker compose up -d --build
} else {
    docker compose up -d
}
Write-Host ""

# ── 3. Wait for postgres ──────────────────────────────────────────────────────
Write-Info "Waiting for postgres..."
for ($i = 1; $i -le 30; $i++) {
    $status = docker inspect --format='{{.State.Health.Status}}' autotrader-postgres 2>&1
    if ($status -eq "healthy") { Write-Ok "postgres healthy"; break }
    if ($i -eq 30) { Write-Err "postgres did not become healthy within 60 s.`n       Check: docker compose logs postgres" }
    Start-Sleep -Seconds 2
}

# ── 4. Wait for redis ─────────────────────────────────────────────────────────
Write-Info "Waiting for redis..."
for ($i = 1; $i -le 30; $i++) {
    $status = docker inspect --format='{{.State.Health.Status}}' autotrader-redis 2>&1
    if ($status -eq "healthy") { Write-Ok "redis healthy"; break }
    if ($i -eq 30) { Write-Err "redis did not become healthy within 60 s.`n       Check: docker compose logs redis" }
    Start-Sleep -Seconds 2
}

# ── 5. Wait for backend API ────────────────────────────────────────────────────
Write-Info "Waiting for backend API (up to 90 s)..."
for ($i = 1; $i -le 45; $i++) {
    try {
        $resp = Invoke-WebRequest -Uri "http://localhost:8000/api/health" -UseBasicParsing -TimeoutSec 2 -ErrorAction SilentlyContinue
        if ($resp.StatusCode -eq 200) { Write-Ok "backend API is up"; break }
    } catch { }
    if ($i -eq 45) { Write-Warn "Backend health check timed out — migrations will still be attempted.`n       If it keeps failing: docker compose logs backend" }
    Start-Sleep -Seconds 2
}

# ── 6. Database migrations ─────────────────────────────────────────────────────
Write-Info "Applying database migrations..."
docker compose exec -T backend alembic upgrade head
Write-Ok "Migrations applied"

# ── 7. Summary ────────────────────────────────────────────────────────────────
$envLines = Get-Content ".env" | Where-Object { $_ -match '^PAPER_TRADING=' }
$paper = if ($envLines) { ($envLines[0] -split '=')[1].Trim() } else { "unknown" }

Write-Host ""
Write-Host "  ╔══════════════════════════════════════════╗" -ForegroundColor Green
Write-Host "  ║   AutoTrader is running                  ║" -ForegroundColor Green
Write-Host "  ╚══════════════════════════════════════════╝" -ForegroundColor Green
Write-Host ""
Write-Host "  Mode:       $paper"        -ForegroundColor White
Write-Host "  Dashboard:  http://localhost:4200"           -ForegroundColor White
Write-Host "  API health: http://localhost:8000/api/health" -ForegroundColor White
Write-Host "  API docs:   http://localhost:8000/docs"       -ForegroundColor White
Write-Host ""
Write-Host "  Groww auth (one-time): POST http://localhost:8000/api/auth/groww/login" -ForegroundColor Yellow
Write-Host "  (only needed once — TOTP tokens do not expire)" -ForegroundColor Yellow
Write-Host ""
Write-Host "  Useful commands:"                                      -ForegroundColor DarkGray
Write-Host "    docker compose logs -f backend                   # live backend logs" -ForegroundColor DarkGray
Write-Host "    docker compose logs -f backend | Select-String scanner  # scanner only" -ForegroundColor DarkGray
Write-Host "    .\start.ps1 -Stop                                # graceful shutdown" -ForegroundColor DarkGray
Write-Host ""

docker compose ps

Pop-Location
