# -----------------------------------------------------------------------------
# AutoTrader single launch script (Windows / local dev)
#
# Works with Podman (preferred) or Docker. The engine is auto-detected:
# podman if it is on PATH, otherwise docker. Override with -Engine or the
# CONTAINER_ENGINE environment variable.
#
# Usage:
#   .\start.ps1                 # start all services (no image rebuild)
#   .\start.ps1 -Build          # rebuild images first (use after code changes)
#   .\start.ps1 -Stop           # gracefully stop all services
#   .\start.ps1 -Status         # print container status and exit
#   .\start.ps1 -Engine docker  # force a specific container engine
#
# Podman notes:
#   - Needs a running Podman machine; this script starts it if it is stopped.
#   - `podman compose` needs a compose provider: docker-compose (recommended,
#     `winget install Docker.DockerCompose`) or podman-compose
#     (`pip install podman-compose`).
# -----------------------------------------------------------------------------
[CmdletBinding()]
param(
    [switch]$Build,
    [switch]$Stop,
    [switch]$Status,
    [ValidateSet("auto", "podman", "docker")]
    [string]$Engine = $(if ($env:CONTAINER_ENGINE) { $env:CONTAINER_ENGINE } else { "auto" })
)

$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot

# -- Colour helpers -----------------------------------------------------------
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

# Run a native command without letting stderr chatter trip $ErrorActionPreference.
# Returns $true when the command exits 0.
function Test-Native {
    param([scriptblock]$Cmd)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { $null = & $Cmd 2>&1 } catch { }
    $ok = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = $prev
    return $ok
}

# -- Resolve container engine -------------------------------------------------
if ($Engine -eq "auto") {
    if (Get-Command podman -ErrorAction SilentlyContinue)     { $Engine = "podman" }
    elseif (Get-Command docker -ErrorAction SilentlyContinue) { $Engine = "docker" }
    else { Write-Err "Neither podman nor docker found on PATH. Install Podman Desktop (https://podman-desktop.io) or Docker Desktop." }
} elseif (-not (Get-Command $Engine -ErrorAction SilentlyContinue)) {
    Write-Err "$Engine not found on PATH."
}

if ($Engine -eq "podman") {
    # Silence the ">>>> Executing external compose provider" banner podman prints.
    $env:PODMAN_COMPOSE_WARNING_LOGS = "false"
}

function Invoke-Compose {
    & $Engine compose @args
    if ($LASTEXITCODE -ne 0) { Write-Err "'$Engine compose $args' failed (exit $LASTEXITCODE)." }
}

# -- Make sure the engine is reachable ----------------------------------------
function Confirm-EngineRunning {
    if ($Engine -eq "podman") {
        if (-not (Test-Native { podman info })) {
            Write-Info "Podman machine is not running - starting it..."
            $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
            podman machine start 2>&1 | Out-Host
            $ErrorActionPreference = $prev
            if (-not (Test-Native { podman info })) {
                Write-Err "Podman machine could not be started. Run 'podman machine list' / 'podman machine init' and retry."
            }
        }
        Write-Ok "Podman is running"
    } else {
        if (-not (Test-Native { docker info })) {
            Write-Err "Docker daemon is not running. Start Docker Desktop first."
        }
        Write-Ok "Docker is running"
    }

    if (-not (Test-Native { & $Engine compose version })) {
        if ($Engine -eq "podman") {
            Write-Err ("'podman compose' has no compose provider. Install one, then open a new terminal:`n" +
                       "         winget install Docker.DockerCompose     (recommended)`n" +
                       "         pip install podman-compose               (alternative)")
        }
        Write-Err "'docker compose' is not available. Install the Docker Compose plugin."
    }
}

# -- -Stop --------------------------------------------------------------------
if ($Stop) {
    Write-Banner "Stopping ($Engine)"
    Confirm-EngineRunning
    Invoke-Compose down
    Write-Ok "All containers stopped (database volume preserved)."
    Pop-Location; exit 0
}

# -- -Status ------------------------------------------------------------------
if ($Status) {
    Confirm-EngineRunning
    Invoke-Compose ps
    Pop-Location; exit 0
}

# =============================================================================
Write-Banner "Starting (local dev, $Engine)"

# -- 1. Pre-flight checks -----------------------------------------------------
Write-Info "Running pre-flight checks..."

if (-not (Test-Path ".env")) {
    Write-Err ".env not found. Copy .env.example to .env and fill in your secrets."
}
$envContent = Get-Content ".env" -Raw
if ($envContent -match '<[^>]+>') {
    Write-Err ".env still contains placeholder values. Fill them all in then rerun."
}
Write-Ok ".env looks populated"

Confirm-EngineRunning

# -- 2. Start containers ------------------------------------------------------
# depends_on: condition: service_healthy makes postgres and redis ready before
# the backend starts, so no manual health-wait loops are needed here.
Write-Info "Starting containers$(if ($Build) { ' (with image rebuild)' })..."
if ($Build) {
    Invoke-Compose up -d --build
} else {
    Invoke-Compose up -d
}
Write-Host ""

# -- 3. Wait for backend API --------------------------------------------------
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
    Write-Warn "Backend health check timed out. Migrations will still be attempted. Run: $Engine compose logs backend"
}

# -- 4. Database migrations ---------------------------------------------------
Write-Info "Applying database migrations..."
Invoke-Compose exec -T backend alembic upgrade head
Write-Ok "Migrations applied"

# -- 5. Summary ---------------------------------------------------------------
$envLines = Get-Content ".env" | Where-Object { $_ -match '^PAPER_TRADING=' }
$paper = if ($envLines) { (($envLines[0] -split '=',2)[1] + '').Trim() } else { "unknown" }
if (-not $paper) { $paper = "unknown" }

Write-Host ""
Write-Host "  ============================================" -ForegroundColor Green
Write-Host "  AutoTrader is running ($Engine)" -ForegroundColor Green
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
Write-Host "  Useful commands:"                                                   -ForegroundColor DarkGray
Write-Host "    $Engine compose logs -f backend           # live backend logs"   -ForegroundColor DarkGray
Write-Host "    $Engine compose logs -f backend | Select-String scanner"         -ForegroundColor DarkGray
Write-Host "    .\start.ps1 -Stop                        # graceful shutdown"    -ForegroundColor DarkGray
Write-Host ""

Invoke-Compose ps

Pop-Location
