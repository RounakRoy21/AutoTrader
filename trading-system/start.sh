#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# AutoTrader — single launch script
#
# Usage:
#   ./start.sh           # start all services (no image rebuild)
#   ./start.sh --build   # rebuild images first, then start (use after code changes)
#   ./start.sh --stop    # gracefully stop all services
#   ./start.sh --status  # print container status and exit
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Colour helpers ─────────────────────────────────────────────────────────────
info()    { echo -e "\033[0;34m[INFO]\033[0m  $*"; }
ok()      { echo -e "\033[0;32m[ OK ]\033[0m  $*"; }
warning() { echo -e "\033[0;33m[WARN]\033[0m  $*"; }
error()   { echo -e "\033[0;31m[ERR ]\033[0m  $*" >&2; exit 1; }

banner() {
    echo ""
    echo "  ╔══════════════════════════════════════════╗"
    echo "  ║   AutoTrader — $1"
    echo "  ╚══════════════════════════════════════════╝"
    echo ""
}

# ── Argument parsing ───────────────────────────────────────────────────────────
BUILD_FLAG=""
ACTION="start"   # start | stop | status

for arg in "$@"; do
    case "$arg" in
        --build)  BUILD_FLAG="--build" ;;
        --stop)   ACTION="stop" ;;
        --status) ACTION="status" ;;
        *) error "Unknown argument: $arg  (valid: --build, --stop, --status)" ;;
    esac
done

# ── Compose file selection ─────────────────────────────────────────────────────
COMPOSE_FILES="-f docker-compose.yml"
if [[ -f docker-compose.prod.yml ]]; then
    APP_ENV_VAL=$(grep -E '^APP_ENV=' .env 2>/dev/null | cut -d'=' -f2 | tr -d '[:space:]' || echo "")
    if [[ "${APP_ENV_VAL}" == "production" ]]; then
        COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.prod.yml"
    fi
fi

COMPOSE="docker compose $COMPOSE_FILES"

# ── --stop ─────────────────────────────────────────────────────────────────────
if [[ "$ACTION" == "stop" ]]; then
    banner "Stopping"
    $COMPOSE down
    ok "All containers stopped (database volume preserved)."
    exit 0
fi

# ── --status ──────────────────────────────────────────────────────────────────
if [[ "$ACTION" == "status" ]]; then
    $COMPOSE ps
    exit 0
fi

# ══════════════════════════════════════════════════════════════════════════════
# START
# ══════════════════════════════════════════════════════════════════════════════
banner "Starting"

# ── 1. Pre-flight checks ───────────────────────────────────────────────────────
info "Running pre-flight checks..."

[[ -f .env ]] || error ".env not found.\n       Copy .env.example → .env and fill in your secrets, then rerun."

if grep -qE '<[^>]+>' .env 2>/dev/null; then
    error ".env still contains placeholder values (e.g. <your API Key>).\n       Fill them all in, then rerun."
fi
ok ".env looks populated"

command -v docker &>/dev/null || error "Docker is not installed. Run ./deploy.sh first."
docker info &>/dev/null        || error "Docker daemon is not running. Start it first."
ok "Docker is running"

# ── 2. Start containers ────────────────────────────────────────────────────────
info "Starting containers${BUILD_FLAG:+ (with image rebuild)}..."
# shellcheck disable=SC2086
$COMPOSE up -d $BUILD_FLAG
echo ""

# ── 3. Wait for postgres to be healthy ────────────────────────────────────────
info "Waiting for postgres..."
for i in $(seq 1 30); do
    STATUS=$(docker inspect --format='{{.State.Health.Status}}' autotrader-postgres 2>/dev/null || echo "missing")
    [[ "$STATUS" == "healthy" ]] && { ok "postgres healthy"; break; }
    [[ $i -eq 30 ]] && error "postgres did not become healthy within 60 s.\n       Investigate: docker compose logs postgres"
    sleep 2
done

# ── 4. Wait for redis to be healthy ───────────────────────────────────────────
info "Waiting for redis..."
for i in $(seq 1 30); do
    STATUS=$(docker inspect --format='{{.State.Health.Status}}' autotrader-redis 2>/dev/null || echo "missing")
    [[ "$STATUS" == "healthy" ]] && { ok "redis healthy"; break; }
    [[ $i -eq 30 ]] && error "redis did not become healthy within 60 s.\n       Investigate: docker compose logs redis"
    sleep 2
done

# ── 5. Wait for backend API ────────────────────────────────────────────────────
info "Waiting for backend API (up to 90 s)..."
for i in $(seq 1 45); do
    if curl -sf http://localhost:8000/api/health &>/dev/null; then
        ok "backend API is up"
        break
    fi
    [[ $i -eq 45 ]] && warning "Backend health check timed out — migrations will still be attempted.\n       If it keeps failing, check: docker compose logs backend"
    sleep 2
done

# ── 6. Database migrations ─────────────────────────────────────────────────────
info "Applying database migrations..."
$COMPOSE exec -T backend alembic upgrade head
ok "Migrations applied"

# ── 7. Summary ────────────────────────────────────────────────────────────────
SERVER_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
PAPER=$(grep -E '^PAPER_TRADING=' .env 2>/dev/null | cut -d'=' -f2 | tr -d '[:space:]' || echo "unknown")

echo ""
echo "  ╔══════════════════════════════════════════╗"
echo "  ║   AutoTrader is running                  ║"
echo "  ╚══════════════════════════════════════════╝"
echo ""
echo "  Mode:       ${PAPER:-unknown}"
echo "  Dashboard:  http://${SERVER_IP}"
echo "  API health: http://${SERVER_IP}/api/health"
echo "  API docs:   http://${SERVER_IP}/api/docs"
echo ""
echo "  Kite login: http://${SERVER_IP}/api/auth/kite/login"
echo "  (complete this before 09:15 IST each trading day)"
echo ""
echo "  Useful commands:"
echo "    docker compose logs -f backend          # live backend logs"
echo "    docker compose logs -f backend | grep scanner   # scanner only"
echo "    docker compose exec postgres psql -U autotrader -d autotrader"
echo "    ./start.sh --stop                       # graceful shutdown"
echo ""

$COMPOSE ps
