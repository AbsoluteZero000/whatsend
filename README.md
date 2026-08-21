# whatsend — WhatsApp Scheduler

Schedule and send WhatsApp messages from a browser dashboard, powered by [Whapi.Cloud](https://whapi.cloud).

## The story

This project was born out of a simple problem: we had a place where WhatsApp messages needed to be sent for events on specific days, but we'd often forget or be late.

**v1** was a Python script using `pywhatkit` to send messages automatically. It worked — kinda. But it required a browser with an already signed-in WhatsApp user, was painfully slow, and couldn't run on a server.

We went back to manual sending. Then **v2** appeared: same idea but powered by the Whapi.Cloud API — no browser needed, fast, server-ready. Jobs were configured via a YAML file, which meant editing files and redeploying every time something changed.

**v3** added REST APIs so everything could be configured remotely. Then came the question: how will normal people use this? That's when the web dashboard was born — Jinja2 templates, vanilla JS, zero build step.

And that's where we are today. whatsend is a full-featured WhatsApp scheduler you can deploy on a $5/month server and control from any browser.

## Features

- **Send Now** — Fire off a message immediately with a single click
- **Trigger on-demand** — Create a job that sits ready and fires when you hit "Send Now"
- **One-time scheduling** — Pick a date & time with a native datetime picker
- **Recurring scheduling** — User-friendly UI (Daily, Weekdays, Weekly with multi-day checkboxes, Monthly, Custom cron) — no cron syntax needed
- **Group picker** — Fetches your WhatsApp groups via the API and shows them by name; manual entry also supported
- **File upload** — Attach validated images, videos, PDFs, Office/OpenDocument files, text/CSV/RTF, or ZIP archives (max 50MB)
- **Clone jobs** — Duplicate any job with one click
- **Skip jobs** — Skip the next scheduled execution (or multiple) without cancelling
- **Search & filter** — Filter by status (Active/Paused/Completed/Failed/All) and search by label or group name
- **Sortable columns** — Click column headers to sort by label, group, trigger, status, or created date
- **Pagination** — 25 jobs per page with numbered page navigation
- **Edit jobs** — Modify any pending/active/trigger job's configuration
- **Execution logs** — Expandable response viewer with pretty-printed JSON
- **Per-user timezone** — Set during signup, change anytime via the nav badge
- **Dark mode** — Toggle via nav button, persisted to localStorage, no flash on load
- **Arabic (RTL) support** — Full Arabic translation and right-to-left layout
- **Keyboard shortcuts** — `n` for new job, `/` to focus search
- **Token encryption** — API tokens encrypted at rest with a separately rotatable Fernet key
- **Reliable scheduler** — Jobs survive restarts, retry transient failures, and retain local recurring times across DST changes
- **Recipient attempts** — Multi-group sends track and retry failed recipients independently
- **Security controls** — CSRF protection, CSP, secure production cookies, rate limits, and account-scoped resources
- **Admin dashboard** — Platform-wide delivery statistics and a searchable user directory with account activation controls

## Stack

| Piece | What |
|---|---|
| Backend | **FastAPI** (async Python 3.13) |
| Database | **SQLite** via **SQLAlchemy 2.0** (async) |
| Frontend | **Jinja2** + **CSS** + vanilla JS (HTMX 2.0 for form submission) |
| i18n | Custom dictionary (English / Arabic with RTL layout) |
| Auth | JWT in httpOnly cookies, bcrypt hashing |
| Crypto | **Fernet** symmetric encryption at rest |
| Scheduler | **APScheduler** (AsyncIOScheduler) |
| Deploy | **Docker** → **Fly.io** (persistent volume, always-on) |

## Schema

```
users ──1:N── tokens ──1:N── jobs ──1:N── logs
                              └──1:N── delivery_attempts
  └──1:N── api_keys
```

- **user** — `id, username, password_hash, timezone, lang (en/ar), is_active, is_admin`
- **token** — `id, user_id, name, api_token (encrypted), is_active, last_used_at`
- **api_key** — `id, user_id, name, key_prefix, key_hash, is_active, last_used_at`
- **job** — includes the trigger, IANA schedule timezone, retry state, media path, and recipient groups
- **log** — `id, job_id, status (sent/failed/skipped), response, sent_at`
- **delivery_attempt** — per-recipient status, run ID, attempt number, response, and timestamp

## Quick start

```bash
cp .env.example .env          # edit SECRET_KEY
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python run.py
```

Open http://localhost:8000, sign up, add a Whapi.Cloud token, and create your first message.

On first startup, the app creates an administrator with username `admin` and password `admin` if that username does not already exist. The first sign-in is restricted to the profile page until the default password is replaced with one of at least 10 characters. Set `DEFAULT_ADMIN_PASSWORD` before first startup to override the bootstrap password, and use `ADMIN_USERNAMES` for additional administrator usernames.

## Optional external API

API-key management and bearer API routes are disabled by default. Set `API_KEYS_ENABLED=true` to register `/api-keys`, `/api/send`, and `/api/groups`.

Generate an API key from `/api-keys`, then send a WhatsApp group message with:

```bash
curl -X POST https://your-app.example.com/api/send \
  -H "Authorization: Bearer wts_1_yourapikey" \
  -H "Content-Type: application/json" \
  -d '{
    "group_id": "120363123456789@g.us",
    "message": "Hello group"
  }'
```

Each API key is linked to one active token owned by the same user. The target `group_id` must be a WhatsApp group ID that connection can send to.

## Supported direct Meta integration spike

The repository includes a `MetaCloudSender` and signature-verified webhook scaffold for evaluating Meta's supported WhatsApp Business Platform Cloud API. Keep it disabled until group eligibility and the account-specific event contract have been verified with a Meta test business account. Reverse-engineered WhatsApp Web protocols are intentionally not implemented because they are unsupported, fragile, and prohibited by WhatsApp's terms.

## Directory layout

```
whatsend/
├── app/
│   ├── main.py               # FastAPI app, lifespan, Jinja2 env, render(), template helpers
│   ├── config.py             # Settings + TIMEZONE_CHOICES
│   ├── database.py           # SQLAlchemy async engine + session
│   ├── i18n.py               # Translation dictionary (en/ar)
│   ├── models/               # User, Token, Job, Log (SQLAlchemy 2.0)
│   ├── routers/              # auth, dashboard, tokens, jobs, logs, about
│   ├── services/             # auth (JWT/bcrypt), crypto (Fernet), sender (Whapi.Cloud), scheduler (APScheduler)
│   ├── templates/            # Jinja2 (base, auth, dashboard, jobs, tokens, logs, about)
│   └── static/css/           # app.css (light + dark themes, 270 lines)
├── migrations/               # Alembic versioned database migrations
├── tests/                    # unit and security regression tests
├── uploads/                  # private uploaded media (auto-created)
├── Dockerfile                # Python 3.13-slim
├── fly.toml                  # Fly.io config (persistent volume at /data)
├── .env                      # SECRET_KEY (not committed)
└── run.py                    # uvicorn entry point
```

## Deploy to Fly.io

```bash
fly launch
fly secrets set SECRET_KEY="your-secret-key"
fly secrets set TOKEN_ENCRYPTION_KEY="a-separate-long-random-secret"
fly volumes create data --region iad --size 1
fly deploy
```

The app uses a persistent 1GB volume at `/data` for SQLite. `auto_stop_machines = false` keeps the scheduler running 24/7. Alembic migrations run as the Fly release command and readiness checks cover both the database and scheduler.

This deployment must remain at one application process/machine while it uses SQLite and the in-process scheduler. Before horizontal scaling, move the scheduler to a single elected worker or durable queue and migrate shared state to PostgreSQL. Keep periodic volume snapshots and test database restores.

For application-level SQLite backups, run `python scripts/backup_database.py`. It uses SQLite's consistent backup API and retains the newest 14 copies by default; configure `BACKUP_DIR` and `BACKUP_RETENTION` as needed.
