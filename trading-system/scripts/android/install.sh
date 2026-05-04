#!/bin/bash
# install.sh — one-time AutoTrader setup inside Ubuntu (proot-distro)
#
# Usage:
#   bash install.sh <git-repo-url>
#
# What it does:
#   1. Installs all system packages (Python, PostgreSQL, Redis, nginx, Node.js)
#   2. Creates the PostgreSQL user + database
#   3. Clones the repo
#   4. Creates .env from .env.example (you fill it in after)
#   5. Creates Python venv + installs deps + runs DB migrations
#   6. Builds the Angular frontend
#   7. Sets up nginx to serve the frontend
#   8. Creates the log directory and makes scripts executable

set -euo pipefail

REPO_URL="${1:-}"
INSTALL_DIR=/root/autotrader
BACKEND_DIR="$INSTALL_DIR/trading-system/backend"
FRONTEND_DIR="$INSTALL_DIR/trading-system/frontend"
SCRIPTS_DIR="$INSTALL_DIR/trading-system/scripts/android"
WEBROOT=/var/www/autotrader

PG_USER=autotrader
PG_PASS=changeme_postgres_password
PG_DB=autotrader

# ─────────────────────────────────────────────────────────────────────────────
step() { echo; echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"; echo "  $*"; echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"; }
ok()   { echo "  ✓ $*"; }
info() { echo "  → $*"; }
fail() { echo; echo "  ✗ ERROR: $*"; exit 1; }

if [ -z "$REPO_URL" ]; then
    fail "Usage: bash install.sh <git-repo-url>"
fi

# ─────────────────────────────────────────────────────────────────────────────
step "Step 1/8 — System packages"

apt-get update -qq
apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    postgresql postgresql-client \
    redis-server \
    nginx \
    git curl wget nano tmux \
    ca-certificates gnupg \
    build-essential libpq-dev \
    > /dev/null

ok "Core packages installed"

# Node.js 20 via NodeSource
if ! command -v node &>/dev/null; then
    info "Installing Node.js 20..."
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - > /dev/null
    apt-get install -y --no-install-recommends nodejs > /dev/null
fi
ok "Node.js $(node --version)"

# ─────────────────────────────────────────────────────────────────────────────
step "Step 2/8 — PostgreSQL"

PG_VER=$(pg_lsclusters --no-header 2>/dev/null | awk '{print $1; exit}')
[ -z "$PG_VER" ] && fail "No PostgreSQL cluster found after install — something went wrong"

# Start PostgreSQL
PG_STATUS=$(pg_ctlcluster "$PG_VER" main status 2>&1 || true)
if ! echo "$PG_STATUS" | grep -q "online"; then
    pg_ctlcluster "$PG_VER" main start
    sleep 2
fi
ok "PostgreSQL $PG_VER running"

# Create user + database (idempotent)
su -c "psql -tc \"SELECT 1 FROM pg_roles WHERE rolname='$PG_USER'\"" postgres \
    | grep -q 1 || su -c "psql -c \"CREATE USER $PG_USER WITH PASSWORD '$PG_PASS'\"" postgres
su -c "psql -tc \"SELECT 1 FROM pg_database WHERE datname='$PG_DB'\"" postgres \
    | grep -q 1 || su -c "psql -c \"CREATE DATABASE $PG_DB OWNER $PG_USER\"" postgres

ok "Database '$PG_DB' and user '$PG_USER' ready"

# ─────────────────────────────────────────────────────────────────────────────
step "Step 3/8 — Redis"

redis-server --daemonize yes \
             --logfile /var/log/redis/redis-server.log \
             --save 900 1 2>/dev/null || true
sleep 1
redis-cli ping | grep -q PONG || fail "Redis did not start"
ok "Redis running"

# ─────────────────────────────────────────────────────────────────────────────
step "Step 4/8 — Clone repository"

if [ -d "$INSTALL_DIR/.git" ]; then
    info "Repo already exists at $INSTALL_DIR — pulling latest..."
    git -C "$INSTALL_DIR" pull
else
    git clone "$REPO_URL" "$INSTALL_DIR"
fi
ok "Repo at $INSTALL_DIR"

# ─────────────────────────────────────────────────────────────────────────────
step "Step 5/8 — Environment file"

ENV_FILE="$INSTALL_DIR/trading-system/.env"
EXAMPLE_FILE="$INSTALL_DIR/trading-system/.env.example"

if [ -f "$ENV_FILE" ]; then
    ok ".env already exists — skipping (edit manually if needed)"
else
    if [ -f "$EXAMPLE_FILE" ]; then
        cp "$EXAMPLE_FILE" "$ENV_FILE"
    else
        # Create a minimal stub
        cat > "$ENV_FILE" << 'ENVEOF'
DATABASE_URL=postgresql+asyncpg://autotrader:changeme_postgres_password@localhost/autotrader
REDIS_URL=redis://localhost:6379
GROWW_CLIENT_ID=
GROWW_PASSWORD=
GROWW_TOTP_SECRET=
ANTHROPIC_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
PAPER_TRADING=true
ADMIN_API_KEY=change_me_to_something_random
ENVEOF
    fi

    # Patch DATABASE_URL and REDIS_URL to point to localhost (not Docker service names)
    sed -i 's|DATABASE_URL=postgresql+asyncpg://.*|DATABASE_URL=postgresql+asyncpg://autotrader:changeme_postgres_password@localhost/autotrader|' "$ENV_FILE"
    sed -i 's|REDIS_URL=redis://.*|REDIS_URL=redis://localhost:6379|' "$ENV_FILE"

    ok ".env created at $ENV_FILE"
    echo
    echo "  ⚠ ACTION REQUIRED: edit $ENV_FILE and fill in:"
    echo "    GROWW_CLIENT_ID / GROWW_PASSWORD / GROWW_TOTP_SECRET"
    echo "    ANTHROPIC_API_KEY"
    echo "    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID"
    echo "    ADMIN_API_KEY  (set to any random string)"
    echo
    echo "  Press Enter when done, or Ctrl-C to exit and edit now..."
    read -r _
fi

# ─────────────────────────────────────────────────────────────────────────────
step "Step 6/8 — Python environment + DB migrations"

cd "$BACKEND_DIR"

if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
source .venv/bin/activate

pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
ok "Python dependencies installed"

# Export DATABASE_URL for alembic
set -a
source "$INSTALL_DIR/trading-system/.env"
set +a

alembic upgrade head
ok "Database migrations applied"

# ─────────────────────────────────────────────────────────────────────────────
step "Step 7/8 — Frontend build + nginx"

cd "$FRONTEND_DIR"
info "Installing npm dependencies (this may take a few minutes)..."
npm install --prefer-offline --loglevel error

info "Building Angular app (production)..."
npx ng build --configuration production

# The @angular-devkit/build-angular:application builder outputs to
# dist/autotrader-dashboard/browser/
DIST_DIR="$FRONTEND_DIR/dist/autotrader-dashboard/browser"
if [ ! -d "$DIST_DIR" ]; then
    # Fallback: some versions omit the nested browser/ dir
    DIST_DIR="$FRONTEND_DIR/dist/autotrader-dashboard"
fi
[ -d "$DIST_DIR" ] || fail "Build output not found. Check the build output above."

mkdir -p "$WEBROOT"
cp -r "$DIST_DIR/"* "$WEBROOT/"
ok "Frontend deployed to $WEBROOT"

# Install the nginx config (overrides the default config)
cp "$SCRIPTS_DIR/nginx.conf" /etc/nginx/nginx.conf
nginx -t 2>/dev/null || fail "nginx config test failed"

# Stop any running nginx, start with the new config
nginx -s quit 2>/dev/null || true
sleep 1
nginx -c /etc/nginx/nginx.conf
ok "nginx running (dashboard at http://localhost:4201)"

# ─────────────────────────────────────────────────────────────────────────────
step "Step 8/8 — Finalize"

mkdir -p /root/autotrader/logs

chmod +x "$SCRIPTS_DIR/startup.sh"
chmod +x "$SCRIPTS_DIR/shutdown.sh"
chmod +x "$SCRIPTS_DIR/scheduler.py"
ok "Scripts made executable"

echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Installation complete!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo
echo "  Next steps:"
echo
echo "  1. Verify your .env file:"
echo "       nano $ENV_FILE"
echo
echo "  2. Exit Ubuntu (type 'exit') and go back to Termux."
echo
echo "  3. Run the bootstrap script to start the scheduler:"
echo "       bash $SCRIPTS_DIR/../../../scripts/android/bootstrap.sh"
echo "     (or, inside the repo: bash trading-system/scripts/android/bootstrap.sh)"
echo
echo "  From now on: the scheduler wakes at 05:30 and 16:00 IST"
echo "  on every trading day and manages everything automatically."
echo
