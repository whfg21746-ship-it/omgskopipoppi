# DexScreener Alerter + X Community Scraper

Unified service that monitors DexScreener for new tokens with X community links, sends Telegram alerts, and automatically scrapes community members.

## Components

| Component | Description |
|-----------|-------------|
| **Alerter** | Polls DexScreener API every 30s, filters tokens by chain/mcap/liquidity/age, detects X community URLs |
| **Scraper** | Headless Playwright browser that scrolls through X community member pages and collects usernames |
| **Telegram Bot** | Unified control interface — alerts, status, data export, runtime configuration |

## Quick Start

```bash
# 1. Clone & install
pip install -r requirements.txt
playwright install chromium

# 2. Configure
cp .env.example .env
# Edit .env with your values

# 3. Run
python main.py
```

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/status` | Service stats (tasks, usernames, uptime) |
| `/tasks` | Last 10 scrape tasks with statuses |
| `/export <community_id>` | Download usernames as .txt |
| `/export_all` | Download ALL unique usernames |
| `/retry <task_id>` | Re-run a failed task |
| `/add <community_url>` | Manually queue a community (no delay) |
| `/token <auth_token>` | Update X auth token at runtime |

## Architecture

```
main.py  ─── asyncio.gather ─┬─ Alerter loop (DexScreener API)
                              ├─ Scraper loop (Playwright in thread)
                              └─ Telegram bot (getUpdates polling)
```

- Alerter detects token → sends alert → creates scrape task with 1h delay
- Scraper picks up ready tasks → launches headless browser → saves to SQLite
- All data persisted in `data/scraper.db` (SQLite) and `data/memory.json`

## Configuration

All settings via `.env` file — see `.env.example` for reference.
