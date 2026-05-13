# WhatSend Architecture

## Overview

WhatSend is a full-stack web application for scheduling WhatsApp messages via the [Whapi.Cloud](https://whapi.cloud) API. Users sign up, add API tokens, pick a group from their WhatsApp account, and schedule messages — one-time, recurring, or send immediately.

```
Browser ──HTTPS──> FastAPI ──SQLAlchemy──> SQLite
                        │
                        ├── APScheduler (AsyncIOScheduler)
                        │       │
                        │       └── send_job() ──httpx──> Whapi.Cloud API
                        │
                        └── Jinja2 ──> HTML (no client-side JS framework)
```

## Stack

| Layer | Technology |
|---|---|
| Web framework | FastAPI (async Python) |
| Database | SQLite via SQLAlchemy 2.0 (async) |
| ORM | SQLAlchemy 2.0 declarative models with `async_session` |
| Templating | Jinja2 (standalone `Environment`, not Starlette's `Jinja2Templates`) |
| Frontend | HTMX 2.0 — no build step, no JS framework |
| Auth | JWT in httpOnly cookies, bcrypt password hashing |
| Crypto | Fernet symmetric encryption (key derived from `SECRET_KEY`) |
| Scheduler | APScheduler `AsyncIOScheduler` running inside the FastAPI process |
| HTTP client | `httpx.AsyncClient` for Whapi.Cloud API calls |
| Config | `pydantic-settings` with `.env` file |
| Deploy | Docker → Fly.io (persistent volume, always-on) |

## Directory layout

```
whatsend/
├── app/
│   ├── main.py               # FastAPI app creation, Jinja2 env, lifespan, route mounting
│   ├── config.py             # Settings + TIMEZONE_CHOICES
│   ├── database.py           # AsyncEngine, async_session, create_tables(), _migrate()
│   ├── models/               # SQLAlchemy 2.0 ORM models
│   │   ├── user.py           # User(id, username, password_hash, timezone, ...)
│   │   ├── token.py          # Token(id, user_id, name, api_token (encrypted), ...)
│   │   ├── job.py            # Job(id, user_id, token_id, group_id, trigger_type, ...)
│   │   └── log.py            # Log(id, job_id, status, response, ...)
│   ├── routers/              # Route handlers (one per page)
│   │   ├── auth.py           # Signup, signin, signout, timezone change
│   │   ├── dashboard.py      # Dashboard stats, recent jobs/logs
│   │   ├── tokens.py         # CRUD for Whapi.Cloud API tokens
│   │   ├── jobs.py           # CRUD for scheduled jobs + group fetching
│   │   ├── logs.py           # Execution log viewer
│   │   └── about.py          # Help page
│   ├── services/             # Business logic
│   │   ├── auth.py           # JWT creation/verification, bcrypt hashing
│   │   ├── crypto.py         # Fernet encrypt/decrypt for tokens at rest
│   │   ├── sender.py         # Async Whapi.Cloud HTTP client (text/image/groups)
│   │   └── scheduler.py      # APScheduler job registration, send_job(), load_all_jobs()
│   ├── templates/            # Jinja2 templates
│   ├── static/               # CSS, favicon
│   └── __init__.py
├── docs/                     # Documentation
├── tests/                    # pytest tests
├── uploads/                  # Uploaded images (auto-created)
├── Dockerfile
├── fly.toml
└── .env                      # SECRET_KEY (not committed)
```

## Request lifecycle

```
1. Request arrives at FastAPI
2. Router handler extracts user from JWT cookie (if any)
3. Handler queries SQLite via async_session
4. Renders Jinja2 template with context
5. Returns HTML response
```

### auth flow

- `POST /auth/signup` — creates user with bcrypt-hashed password, returns JWT cookie
- `POST /auth/signin` — verifies password, returns JWT cookie
- JWT payload: `{sub: user_id, username, tz}`
- Cookie: httpOnly, SameSite=Lax, 24h expiry
- `require_user()` extracts user from cookie or raises `RedirectRequired`

### RedirectRequired exception

FastAPI responses cannot be `raise`d inside handler dependencies (they must be `return`ed). To handle auth redirects in `require_user()` (which is called inside handlers), we raise a custom `RedirectRequired` exception and catch it with a global `@app.exception_handler`. This allows a simple `user = require_user(request)` pattern in every protected route.

## Database

### Models

```
users
  id            INTEGER PRIMARY KEY
  username      VARCHAR(80) UNIQUE
  password_hash VARCHAR(255)
  timezone      VARCHAR(64)    DEFAULT 'UTC'
  is_active     BOOLEAN        DEFAULT TRUE
  created_at    DATETIME       DEFAULT NOW

tokens
  id            INTEGER PRIMARY KEY
  user_id       INTEGER → users.id
  name          VARCHAR(100)
  api_token     TEXT           (Fernet-encrypted)
  is_active     BOOLEAN        DEFAULT TRUE
  last_used_at  DATETIME
  created_at    DATETIME       DEFAULT NOW

jobs
  id            INTEGER PRIMARY KEY
  user_id       INTEGER → users.id
  token_id      INTEGER → tokens.id
  label         VARCHAR(200)
  group_id      VARCHAR(100)
  message       TEXT
  image_path    TEXT
  trigger_type  VARCHAR(10)    'now' | 'date' | 'cron'
  trigger_value VARCHAR(100)   datetime string or cron expression
  status        VARCHAR(10)    'pending' | 'active' | 'completed' | 'cancelled'
  created_at    DATETIME       DEFAULT NOW

logs
  id            INTEGER PRIMARY KEY
  job_id        INTEGER → jobs.id
  status        VARCHAR(10)    'sent' | 'failed'
  response      TEXT           pretty-printed JSON API response
  sent_at       DATETIME       DEFAULT NOW
```

### Migrations

The `_migrate()` function in `database.py` runs on startup and applies schema changes that are safe for existing databases (e.g. `ALTER TABLE ADD COLUMN` for nullable columns with defaults). This avoids the need for a formal migration tool while supporting rolling deploys.

## Scheduling system

The `AsyncIOScheduler` runs inside the FastAPI process, started in the `lifespan` context manager.

### Startup

1. FastAPI starts
2. `lifespan()` runs:
   a. `create_tables()` — ensures DB schema exists, runs migrations
   b. `apscheduler.start()` — starts the scheduler
   c. `load_all_jobs()` — queries DB for all `pending`/`active` jobs and registers them with APScheduler

### Job registration

`register_job()` in `app/services/scheduler.py`:

| trigger_type | APScheduler trigger | Notes |
|---|---|---|
| `now` | `DateTrigger` with +2s delay | Fires immediately after creation |
| `date` | `DateTrigger` at parsed datetime | Times are stored in UTC |
| `cron` | `CronTrigger` parsed from expression | Cron expressions are generated by the UI (Daily/Weekdays/Weekly/Monthly) |

### Execution

When a trigger fires, APScheduler calls `send_job(job_id)`:
1. Loads the job and its token from DB
2. Decrypts the token with Fernet
3. Calls `WhatsAppSender.send()` (async HTTP to Whapi.Cloud)
4. Creates a `Log` entry with the result
5. Updates job status (completed for one-time, stays active for cron)

## Timezone handling

- User selects a timezone during signup (stored in `users.timezone` and JWT `tz` field)
- "One-time" date input is converted from user's timezone to UTC before storing
- Stored trigger values are always in UTC
- `tz()` Jinja2 filter converts UTC datetimes to the user's timezone for display
- `parse_dt()` converts stored datetime strings to Python datetime objects
- Users can change timezone at any time via `/auth/timezone`

## Token encryption

API tokens are encrypted at rest using Fernet symmetric encryption:

1. `SECRET_KEY` from `.env` is hashed with SHA-256
2. The hash is base64-encoded to produce a 32-byte Fernet key
3. `encrypt_token()` encrypts the plaintext token before storing in DB
4. `decrypt_token()` decrypts it at send time

## Image upload

- Accepted types: JPEG, PNG, GIF, WebP
- Max size: 5MB
- Images are saved to `uploads/` with a unique timestamp-based filename
- Image file is deleted when the associated job is deleted
- Sent as `multipart/form-data` to Whapi.Cloud's `/messages/image` endpoint

## Group picker

The job creation form calls `WhatsAppSender.get_groups()` which hits Whapi.Cloud's `GET /groups` endpoint. Groups are displayed as a `<select>` dropdown showing group names. A "Manual entry" option allows typing a group ID directly.

## Scheduler persistence

### Local development

The scheduler runs inside the same process as the web server. Jobs are stored in SQLite and reloaded on every startup.

### Production (Fly.io)

- `auto_stop_machines = false` and `min_machines_running = 1` keep the VM running 24/7
- SQLite database lives on a persistent 1GB volume at `/data`
- The scheduler restarts jobs from the DB if the machine is restarted

## UI components

### Dashboard (`/dashboard`)
- Time-based greeting
- 6 stat cards: Total Jobs, Active, Sent, Failed, Success Rate, Tokens
- Success rate progress bar
- Recent Jobs / Recent Activity mini-tables

### Job form (`/jobs/create`)
- Token selector → groups fetched from Whapi.Cloud
- Message text + optional image upload
- Trigger: Send Now / One-time (native datetime picker) / Recurring (friendly UI)
- Recurring options: Daily, Weekdays, Weekly (single or custom multi-day), Monthly

### Logs (`/logs`)
- Filterable by job ID
- Expandable response details via `<details>/<summary>`
- Pretty-printed JSON responses
- Timezone-aware timestamps

## Jinja2 compatibility note

Starlette's `Jinja2Templates` has a compatibility bug with Jinja2 3.1.6+ (`unhashable` cache key error). The workaround is to use a raw Jinja2 `Environment` with `cache_size=0` and a custom `render()` function that handles `user_tz` injection.

## Deployment

### Dockerfile

```dockerfile
FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt . && pip install -r requirements.txt
COPY . .
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### Fly.io

`fly.toml` configures a persistent volume at `/data` and sets `DATABASE_URL` to point there. The `release_command` runs `create_tables()` on every deploy to ensure the schema is up to date.

`SECRET_KEY` must be set as a Fly secret:
```bash
fly secrets set SECRET_KEY="your-secret-key"
```
