# WhatSend — WhatsApp Scheduler

Schedule and send WhatsApp messages from a browser dashboard, powered by [Whapi.Cloud](https://whapi.cloud).

## Features

- **Send Now** — Fire off a message immediately with a single click
- **One-time scheduling** — Pick a date & time with a native datetime picker
- **Recurring scheduling** — User-friendly UI (Daily, Weekdays, Weekly with multi-day checkboxes, Monthly) — no cron syntax needed
- **Group picker** — Fetches your WhatsApp groups via the API and shows them by name; manual entry also supported
- **Image upload** — Attach JPEG/PNG/GIF/WebP (max 5MB)
- **Per-user timezone** — Set during signup, change anytime via the nav badge
- **Execution logs** — Expandable response viewer with pretty-printed JSON
- **Token encryption** — API tokens encrypted at rest with Fernet (key derived from `SECRET_KEY`)
- **Persistent scheduler** — Jobs survive app restarts via APScheduler + SQLite

## Stack

| Piece | What |
|---|---|
| Backend | **FastAPI** (async Python 3.13) |
| Database | **SQLite** via **SQLAlchemy 2.0** (async) |
| Frontend | **Jinja2** + **HTMX** (no JS build step) |
| Auth | JWT in httpOnly cookies, bcrypt hashing |
| Crypto | **Fernet** symmetric encryption at rest |
| Scheduler | **APScheduler** (AsyncIOScheduler) |
| Deploy | **Docker** → **Fly.io** (persistent volume, always-on) |

## Schema

```
users ──1:N── tokens ──1:N── jobs ──1:N── logs
```

- **user** — `id, username, password_hash, timezone, is_active`
- **token** — `id, user_id, name, api_token (encrypted), is_active, last_used_at`
- **job** — `id, user_id, token_id, label, group_id, message, image_path, trigger_type (now/date/cron), trigger_value, status`
- **log** — `id, job_id, status (sent/failed), response, sent_at`

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
│   ├── main.py               # FastAPI app, lifespan, Jinja2 env, render()
│   ├── config.py             # Settings + TIMEZONE_CHOICES
│   ├── database.py           # SQLAlchemy async engine + session + migration
│   ├── models/               # User, Token, Job, Log (SQLAlchemy 2.0)
│   ├── routers/              # auth, dashboard, tokens, jobs, logs
│   ├── services/             # auth (JWT/bcrypt), crypto (Fernet), sender (Whapi.Cloud), scheduler (APScheduler)
│   ├── templates/            # Jinja2 (base, auth, dashboard, jobs, tokens, logs)
│   └── static/css/           # app.css
├── tests/                    # pytest (test_crypto.py)
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
