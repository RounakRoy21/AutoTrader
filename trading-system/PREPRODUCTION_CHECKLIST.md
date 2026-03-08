# Pre-Production Checklist — AutoTrader

This file summarizes the accounts, configuration steps, and actions required before running AutoTrader in production.

**Accounts to create**

- Zerodha (Kite Connect): Trading account + Kite Connect subscription (create an app to get `KITE_API_KEY` & `KITE_API_SECRET`).
- Oracle Cloud Free Tier: create an account and provision an Ampere A1 instance (Ubuntu).
- Anthropic: API account; obtain `ANTHROPIC_API_KEY`.
- Alpha Vantage: get `ALPHAVANTAGE_API_KEY` (free tier, 25 req/day — used for US close and DXY).
- Telegram: create a bot via @BotFather and get `TELEGRAM_BOT_TOKEN`; find your `TELEGRAM_CHAT_ID`.

> **No longer needed:** NewsAPI. News is now gathered from 5 Indian financial RSS feeds (Economic Times, Business Standard, Moneycontrol, LiveMint, NDTV Profit) + Google News RSS — all free and keyless. India VIX is fetched from Stooq (also free, no key).

**Post-account configuration**

- Zerodha/Kite:
  - Subscribe to Kite Connect and create an app.
  - Set OAuth redirect URL to `http://<your-server-ip>/api/auth/kite/callback`.
  - Keep `KITE_API_KEY` and `KITE_API_SECRET` ready for `.env`.
- Anthropic / AlphaVantage:
  - Place API keys in the backend `.env` file.
- Telegram:
  - Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` to `.env`.

**Infrastructure prerequisites**

- A Linux VM with Docker and Docker Compose (Oracle Cloud Ampere A1 recommended for free tier).
- Open TCP port 80 (and SSH 22) in the cloud VCN / security list.
- Ensure the instance has outbound internet access for API calls.

**Code / Docker changes required before production**

- Use `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build` to run the production stack.
- Ensure `.env` is filled with real keys before first `deploy.sh` run.
- Confirm Alembic migrations are applied: `docker compose exec backend alembic upgrade head`.

**Application-level pre-production tasks**

- [x] Phase 4: Scanner → Decision Engine signal pipeline — **complete**
- [x] Phase 5: Risk Manager (stop-loss / target monitoring, daily drawdown halt, square-off) — **complete**
- [x] Phase 6: Trading Agent orchestration (order placement, persistence, alerts) — **complete**
- [x] Phase 7: Frontend live data (LTP streaming, open-position P&L, agent controls) — **complete**
- [x] Phase 8: Docker + deployment hardening — **complete**
- End-to-end testing in `PAPER_TRADING=true` mode for at least 1–2 weeks.
- Validate Telegram daily Kite re-auth alert and OAuth flow.
- Confirm GTT / order placement behavior in the Zerodha paper environment.

**Before the first live trade (mandatory)**

1. Set `PAPER_TRADING=true` and run the full stack for 1–2 weeks.
2. Use a conservative `TOTAL_CAPITAL` in `.env` (₹50k–₹100k recommended for initial testing).
3. Monitor the dashboard, trade logs, and Telegram alerts for unexpected behaviour.
4. Verify that stop-losses / GTTs placed by the system appear in Zerodha's dashboard.
5. Only switch `PAPER_TRADING=false` after consistent, satisfactory paper-trading results.

**Quick local commands**

```bash
# Build & run dev (local development):
docker compose up --build

# Build & run production (on your server):
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build

# Run migrations:
docker compose exec backend alembic upgrade head

# Trigger Research Agent manually (HTTP):
curl -X POST http://<server-ip-or-localhost>:8000/api/market-brief/run

# Check Research Agent status (HTTP):
curl http://<server-ip-or-localhost>:8000/api/market-brief/status
```

**Notes & recommendations**

- Use Oracle Cloud Free Tier (Ampere A1) for a truly free always-on VM. You will need to allow traffic on port 80 in OCI's VCN rules.
- Keep `PAPER_TRADING=true` until you are fully confident. The system will run end-to-end with paper trades and simulated market ticks.
- Store backups of your database volume and keep a copy of `.env` in a secure password manager.

---

Generated from the team's pre-production discussion on March 3, 2026.
