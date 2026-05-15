# WhatSend — WhatsApp Scheduler

Schedule and send WhatsApp messages from a browser dashboard, powered by [Whapi.Cloud](https://whapi.cloud).

## The story

This project was born out of a simple problem: we had a place where WhatsApp messages needed to be sent for events on specific days, but we'd often forget or be late.

**v1** was a Python script using `pywhatkit` to send messages automatically. It worked — kinda. But it required a browser with an already signed-in WhatsApp user, was painfully slow, and couldn't run on a server.

We went back to manual sending. Then **v2** appeared: same idea but powered by the Whapi.Cloud API — no browser needed, fast, server-ready. Jobs were configured via a YAML file, which meant editing files and redeploying every time something changed.

**v3** added REST APIs so everything could be configured remotely. Then came the question: how will normal people use this? That's when the web dashboard was born — Jinja2 templates, vanilla JS, zero build step.

And that's where we are today. WhatSend is a full-featured WhatsApp scheduler you can deploy on a $5/month server and control from any browser.

## Features

- **Send Now** — Fire off a message immediately with a single click
- **Trigger on-demand** — Create a job that sits ready and fires when you hit "Send Now"
- **One-time scheduling** — Pick a date & time with a native datetime picker
- **Recurring scheduling** — User-friendly UI (Daily, Weekdays, Weekly with multi-day checkboxes, Monthly, Custom cron) — no cron syntax needed
- **Group picker** — Fetches your WhatsApp groups via the API and shows them by name; manual entry also supported
- **Image upload** — Attach JPEG/PNG/GIF/WebP (max 5MB)
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
- **Token encryption** — API tokens encrypted at rest with Fernet (key derived from `SECRET_KEY`)
- **Persistent scheduler** — Jobs survive app restarts via APScheduler + SQLite

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
```

- **user** — `id, username, password_hash, timezone, lang (en/ar), is_active`
- **token** — `id, user_id, name, api_token (encrypted), is_active, last_used_at`
- **job** — `id, user_id, token_id, label, group_id, group_name, message, image_path, trigger_type (now/date/cron/trigger), trigger_value, status, skip_count`
- **log** — `id, job_id, status (sent/failed/skipped), response, sent_at`

## Quick start

```bash
cp .env.example .env          # edit SECRET_KEY
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python run.py
```

Open http://localhost:8000, sign up, add a Whapi.Cloud token, and create your first message.

## Directory layout

```
whatsend/
├── app/
│   ├── main.py               # FastAPI app, lifespan, Jinja2 env, render(), template helpers
│   ├── config.py             # Settings + TIMEZONE_CHOICES
│   ├── database.py           # SQLAlchemy async engine + session + migration
│   ├── i18n.py               # Translation dictionary (en/ar)
│   ├── models/               # User, Token, Job, Log (SQLAlchemy 2.0)
│   ├── routers/              # auth, dashboard, tokens, jobs, logs, about
│   ├── services/             # auth (JWT/bcrypt), crypto (Fernet), sender (Whapi.Cloud), scheduler (APScheduler)
│   ├── templates/            # Jinja2 (base, auth, dashboard, jobs, tokens, logs, about)
│   └── static/css/           # app.css (light + dark themes, 270 lines)
├── tests/                    # pytest (test_crypto.py — 6 tests)
├── uploads/                  # uploaded images (auto-created)
├── Dockerfile                # Python 3.13-slim
├── fly.toml                  # Fly.io config (persistent volume at /data)
├── .env                      # SECRET_KEY (not committed)
└── run.py                    # uvicorn entry point
```

## Deploy to Fly.io

```bash
fly launch
fly secrets set SECRET_KEY="your-secret-key"
fly volumes create data --region iad --size 1
fly deploy
```

The app uses a persistent 1GB volume at `/data` for SQLite. `auto_stop_machines = false` keeps the scheduler running 24/7.
