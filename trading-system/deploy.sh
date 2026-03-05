#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# AutoTrader — Oracle Cloud Free Tier bootstrap script
#
# Run once on a fresh Ubuntu 22.04/24.04 instance:
#   chmod +x deploy.sh && ./deploy.sh
#
# What it does:
#   1. Installs Docker (if missing)
#   2. Installs Docker Compose plugin (if missing)
#   3. Opens the required firewall ports (Oracle Cloud also has VCN rules —
#      see the note at the bottom)
#   4. Copies .env.example → .env if no .env exists yet, then exits so you
#      can fill in your secrets before the first real start
#   5. Builds & starts the full production stack
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

BANNER="═══════════════════════════════════════════"
info()    { echo -e "\033[0;32m[INFO]\033[0m  $*"; }
warning() { echo -e "\033[0;33m[WARN]\033[0m  $*"; }
error()   { echo -e "\033[0;31m[ERR ]\033[0m  $*"; exit 1; }

echo "$BANNER"
echo "   AutoTrader — Production Deploy Bootstrap"
echo "$BANNER"

# ── 1. Docker ─────────────────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
    info "Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker "$USER"
    warning "Docker installed. You may need to log out and back in for group changes."
    warning "If the next step fails with a permissions error, run: newgrp docker"
else
    info "Docker already installed: $(docker --version)"
fi

# ── 2. Docker Compose plugin ──────────────────────────────────────────────────
if ! docker compose version &>/dev/null; then
    info "Installing Docker Compose plugin..."
    sudo apt-get update -qq
    sudo apt-get install -y docker-compose-plugin
else
    info "Docker Compose already installed: $(docker compose version)"
fi

# ── 3. Firewall — open ports 80 (dashboard) and 22 (SSH) ──────────────────────
# Note: Oracle Cloud also has VCN Security List / NSG rules.
# You MUST also open TCP port 80 (and optionally 8000) in the OCI console under:
#   Networking → Virtual Cloud Networks → your VCN → Security Lists → Ingress Rules
info "Configuring host firewall..."
if command -v ufw &>/dev/null && ufw status | grep -q "Status: active"; then
    ufw allow 80/tcp
    ufw allow 22/tcp
    info "ufw rules added."
else
    sudo iptables -C INPUT -p tcp --dport 80  -j ACCEPT 2>/dev/null || \
        sudo iptables -I INPUT -p tcp --dport 80  -j ACCEPT
    sudo iptables -C INPUT -p tcp --dport 22  -j ACCEPT 2>/dev/null || \
        sudo iptables -I INPUT -p tcp --dport 22  -j ACCEPT
    if command -v netfilter-persistent &>/dev/null; then
        sudo netfilter-persistent save
    fi
fi

# ── 4. Environment file ───────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        cp .env.example .env
        warning ".env created from .env.example."
        warning "Edit it now with your real secrets, then re-run this script:"
        warning "  nano .env && ./deploy.sh"
        exit 0
    else
        error ".env and .env.example both missing — cannot continue."
    fi
fi

# Guard: abort if the user hasn't replaced placeholder values
if grep -q "CHANGE_ME" .env; then
    error ".env still contains CHANGE_ME placeholders. Fill in all secrets first:\n  nano .env && ./deploy.sh"
fi

info ".env found and looks configured."

# ── 5. Build & start production stack ────────────────────────────────────────
info "Building and starting production stack..."
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build

# ── 6. Run database migrations ───────────────────────────────────────────────
info "Waiting for PostgreSQL to be ready..."
PG_RETRIES=30
until docker compose exec -T postgres pg_isready -U "${POSTGRES_USER:-autotrader}" &>/dev/null; do
    PG_RETRIES=$((PG_RETRIES - 1))
    if [ "$PG_RETRIES" -le 0 ]; then
        error "PostgreSQL did not become ready in time."
    fi
    echo -n "."
    sleep 2
done
echo ""
info "PostgreSQL is ready. Running Alembic migrations..."
docker compose exec backend alembic upgrade head

# ── 7. Done ───────────────────────────────────────────────────────────────────
SERVER_IP=$(curl -s --max-time 5 ifconfig.me 2>/dev/null || echo "<your-server-ip>")
echo ""
echo "$BANNER"
echo "  AutoTrader is running!"
echo "$BANNER"
echo ""
echo "  Dashboard  :  http://${SERVER_IP}/"
echo "  API docs   :  http://${SERVER_IP}/api/docs"
echo ""
echo "  Daily Kite auth:"
echo "    The Telegram bot will send you a login link at 8:50 AM IST."
echo "    Tap it on your phone → complete OAuth → trading starts automatically."
echo ""
echo "  Useful commands:"
echo "    View logs     : docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f"
echo "    Stop          : docker compose -f docker-compose.yml -f docker-compose.prod.yml down"
echo "    Pull + redeploy: git pull && ./deploy.sh"
echo "$BANNER"
