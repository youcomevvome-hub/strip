# Strip — AI Content Aggregator & Multi-Platform Publisher

Scrape websites → AI structures & summarizes → human validates → auto-posts to every major social platform.

## Two UIs, pick one
- **Built-in HTML UI** (recommended, zero extra setup) — served by the Python backend at `/ui`. No Node.js required.
- **Next.js UI** (optional, prettier) — under `frontend/`. Requires Node.js.

## Quick start (no Node.js, no third-party APIs)

**Easiest** — double-click `run.bat`, or in PowerShell:
```powershell
.\run.ps1
```
It creates the project venv at `.\.venv`, installs every dependency on first run, then starts the server.

**Manual** (equivalent):
```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
Copy-Item backend\.env.example backend\.env
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

Open **http://localhost:8000** → it redirects to `/ui/register` to create the admin account, then to the dashboard.

You can use the app immediately with **zero third-party credentials**:
- Add sources (URL, optional RSS) on **Sources**, click **Scrape now**.
- Approve drafts on **Validation Queue**.
- The default output target is **RSS** (`/rss`) — always free, always works. Plug it into IFTTT, Zapier, Buffer free tier, or any RSS-aware tool to fan out to socials without API keys.

### Optional: local AI (Ollama)
Without Ollama the app uses a built-in deterministic summarizer (works fine, less polished). For better quality:
```powershell
# install Ollama from https://ollama.com
ollama pull llama3.1:8b
ollama serve
```

## Free social channels you can enable today
No company/business API access required — personal accounts are enough:

| Channel  | How to get it |
|----------|---------------|
| **RSS**  | already running at `/rss` |
| **Discord** | server settings → Integrations → Webhooks → copy URL into `DISCORD_WEBHOOK_URL` |
| **Telegram** | chat with `@BotFather` → `/newbot` → put token in `TELEGRAM_BOT_TOKEN` and chat IDs in `TELEGRAM_CHAT_IDS` |
| **Mastodon** | free account on mastodon.social → Preferences → Development → New application → token into `MASTODON_ACCESS_TOKEN` |
| **Reddit**  | reddit.com/prefs/apps → create "script" app → fill `REDDIT_*` vars |

The paid/business platforms (Twitter/X, LinkedIn, Facebook Page, Instagram, WhatsApp) are wired and ready — just paste credentials into `.env` when you have them. Channels with missing credentials are silently **skipped** at publish time, so they never break a post.

## Architecture
```
┌──────────────────────┐    ┌────────────────────────────────────┐    ┌────────────────┐
│ Built-in HTML UI     │───▶│ FastAPI backend                    │───▶│ Social/RSS     │
│  (/ui, Jinja + HTMX) │    │  • Scraper (RSS / HTTP / Playwright)│    │ output targets │
│ Next.js UI (optional)│    │  • AI (Ollama local + fallback)    │    └────────────────┘
└──────────────────────┘    │  • 10 publishers + scheduler       │
                            └────────────────────────────────────┘
```

The backend is **stack-agnostic**: plain REST API documented at `/docs` (OpenAPI). Any frontend or third-party service can integrate by sending `X-API-Key: <your-key>`.

## Components
- **backend/** — FastAPI, SQLAlchemy, APScheduler, httpx, BeautifulSoup, Playwright, Ollama
- **backend/app/templates/** — built-in HTML UI (Jinja + HTMX + Tailwind via CDN)
- **frontend/** — optional Next.js 14 (App Router) UI

## Optional: Next.js frontend
Only if you want the React UI:
```powershell
# install Node.js 20+ first: https://nodejs.org
cd frontend
npm install
Copy-Item .env.local.example .env.local
npm run dev
```
Open http://localhost:3000.

## Optional: Docker (all-in-one)
```powershell
docker compose up --build
```

## Daily flow
1. Add sources on the **Sources** page.
2. APScheduler runs daily at 06:00 UTC (configurable via `SCRAPE_CRON_HOUR`/`MINUTE`).
3. Each new article is scraped, AI-structured, and saved as a `draft`.
4. Open **Validation Queue** — review, edit, approve, pick platforms.
5. Click **Publish** — backend fans out and records results on the **Posts** page.

## Social platform support
| Platform        | Auth                              | Free? |
|-----------------|-----------------------------------|-------|
| RSS feed        | none                              | ✅ always |
| Discord         | webhook URL                       | ✅ |
| Telegram        | bot token + chat IDs              | ✅ |
| Mastodon        | app token                         | ✅ |
| Reddit          | personal script app               | ✅ |
| X / Twitter     | OAuth1 (tweepy)                   | paid API tier |
| LinkedIn        | OAuth2 user token                 | needs developer app |
| Facebook Page   | Page access token                 | needs Meta app |
| Instagram       | Graph API (FB business)           | needs Meta app + image |
| WhatsApp        | Cloud API                         | needs Meta app |

## API for third-party integration
All `/api/*` endpoints accept `X-API-Key: <your-key>` (set `API_KEY` in `.env`). Examples:
```
POST   /api/sources          add a website to track
GET    /api/articles?status=pending
POST   /api/posts/{id}/approve
POST   /api/posts/{id}/publish   { "platforms": ["telegram","discord","rss"] }
```
Full schema at http://localhost:8000/docs.

## Security
- Bcrypt-hashed passwords, signed-cookie sessions for the UI, separate static API key for machine clients.
- All secrets via `.env` — never commit it.
- Scraper sends a descriptive User-Agent and times out cleanly.
