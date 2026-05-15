# WhatSend Architecture

## Overview

WhatSend is a full-stack web application for scheduling WhatsApp messages via the [Whapi.Cloud](https://whapi.cloud) API. Users sign up, add API tokens, pick a group from their WhatsApp account, and schedule messages — one-time, recurring, send immediately, or trigger-on-demand.

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
| Frontend | Jinja2 + CSS + vanilla JS (HTMX 2.0 for form submission) |
| Auth | JWT in httpOnly cookies, bcrypt password hashing |
| Crypto | Fernet symmetric encryption (key derived from `SECRET_KEY`) |
| Scheduler | APScheduler `AsyncIOScheduler` running inside the FastAPI process |
| HTTP client | `httpx.AsyncClient` for Whapi.Cloud API calls |
| Config | `pydantic-settings` with `.env` file |
| i18n | Custom dictionary-based translation system (en/ar) |
| Deploy | Docker → Fly.io (persistent volume, always-on) |

## Directory layout

```
whatsend/
├── app/
│   ├── main.py               # FastAPI app creation, Jinja2 env, render(), lifespan
│   ├── config.py             # Settings + TIMEZONE_CHOICES
│   ├── database.py           # AsyncEngine, async_session, create_tables(), _migrate()
│   ├── i18n.py               # Translation dictionary (_() function, en/ar)
│   ├── models/               # SQLAlchemy 2.0 ORM models
│   │   ├── user.py           # User(id, username, password_hash, timezone, lang, ...)
│   │   ├── token.py          # Token(id, user_id, name, api_token (encrypted), ...)
│   │   ├── job.py            # Job(id, user_id, token_id, group_id, group_name, trigger_type, ...)
│   │   └── log.py            # Log(id, job_id, status, response, ...)
│   ├── routers/              # Route handlers (one per page)
│   │   ├── auth.py           # Signup, signin, signout, timezone, profile, lang toggle
│   │   ├── dashboard.py      # Dashboard stats, recent jobs/logs
│   │   ├── tokens.py         # CRUD for Whapi.Cloud API tokens
│   │   ├── jobs.py           # CRUD, clone, edit, skip, send-now, search, sort, pagination
│   │   ├── logs.py           # Execution log viewer
│   │   └── about.py          # Help page
│   ├── services/             # Business logic
│   │   ├── auth.py           # JWT creation/verification, bcrypt hashing
│   │   ├── crypto.py         # Fernet encrypt/decrypt for tokens at rest
│   │   ├── sender.py         # Async Whapi.Cloud HTTP client (text/image/groups)
│   │   └── scheduler.py      # APScheduler job registration, send_job(), load_all_jobs()
│   ├── templates/            # Jinja2 templates
│   │   ├── base.html         # Layout: nav, footer, clock, theme toggle, lang toggle, modals
│   │   ├── auth/             # signin, signup, timezone, profile
│   │   ├── dashboard/        # index.html (stats cards, progress, recent tables)
│   │   ├── jobs/             # list.html (table with sort/paginate/filter) + form.html
│   │   ├── tokens/           # list.html + form.html
│   │   ├── logs/             # list.html (expandable response viewer)
│   │   └── about/            # index.html (help + project story)
│   ├── static/css/
│   │   └── app.css           # All styles (light/dark themes, 270 lines)
│   └── __init__.py
├── docs/                     # Documentation
├── tests/                    # pytest (test_crypto.py with 6 tests)
├── uploads/                  # Uploaded images (auto-created, path configurable via UPLOAD_DIR)
├── Dockerfile
├── fly.toml
└── .env                      # SECRET_KEY (not committed)
```

## Request lifecycle

```
1. Request arrives at FastAPI
2. Router handler extracts user from JWT cookie (if any)
3. Handler queries SQLite via async_session
4. Renders Jinja2 template with context via custom render() — injects _() and user_tz
5. Returns HTML response
```

### auth flow

- `POST /auth/signup` — creates user with bcrypt-hashed password, sets JWT + lang cookies
- `POST /auth/signin` — verifies password, sets JWT + lang cookies
- JWT payload: `{sub: user_id, username, tz, lang}`
- Cookie: httpOnly, SameSite=Lax, 24h expiry
- `require_user()` extracts user from cookie or raises `RedirectRequired`
- `POST /auth/lang` — toggles `lang` between `en` and `ar`, updates DB + JWT + cookie
- `GET /auth/profile` — profile settings page (change username, password)
- `POST /auth/profile` — updates username and/or password

### RedirectRequired exception

FastAPI responses cannot be `raise`d inside handler dependencies (they must be `return`ed). To handle auth redirects in `require_user()` (which is called inside handlers), we raise a custom `RedirectRequired` exception and catch it with a global `@app.exception_handler`. This allows a simple `user = require_user(request)` pattern in every protected route.

### Flash messages (toast notifications)

Redirects use `?success=` or `?error=` query params (appended via `redirect_with_flash()` / `_flash()` helpers). The JS in `base.html` parses these on page load, displays a toast notification (auto-dismiss after 4s), and cleans the URL via `history.replaceState()`.

Toast styles: `toast-success` (green) and `toast-error` (red), positioned top-right (or top-left in RTL).

## Database

### Models

```
users
  id            INTEGER PRIMARY KEY
  username      VARCHAR(80) UNIQUE
  password_hash VARCHAR(255)
  timezone      VARCHAR(64)    DEFAULT 'UTC'
  lang          VARCHAR(2)     DEFAULT 'en'
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
  label         VARCHAR(120)
  group_id      VARCHAR(255)
  group_name    VARCHAR(255)   (cached group name from Whapi.Cloud)
  message       TEXT
  image_path    TEXT
  trigger_type  VARCHAR(10)    'now' | 'date' | 'cron' | 'trigger'
  trigger_value VARCHAR(100)   datetime string or cron expression
  status        VARCHAR(20)    'pending' | 'active' | 'completed' | 'cancelled' | 'paused' | 'trigger' | 'failed'
  skip_count    INTEGER        DEFAULT 0
  created_at    DATETIME       DEFAULT NOW
  updated_at    DATETIME       DEFAULT NOW (onupdate)

logs
  id            INTEGER PRIMARY KEY
  job_id        INTEGER → jobs.id
  status        VARCHAR(10)    'sent' | 'failed' | 'skipped'
  response      TEXT           pretty-printed JSON API response
  sent_at       DATETIME       DEFAULT NOW
```

### Migrations

The `_migrate()` function in `database.py` runs on startup and applies schema changes that are safe for existing databases (e.g. `ALTER TABLE ADD COLUMN` for nullable columns with defaults). This avoids the need for a formal migration tool while supporting rolling deploys. Current migrations handle: `timezone`, `group_name`, `skip_count`, `lang`.

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
- Skips jobs with `trigger_type == "trigger"` (they are never scheduled — triggered manually via "Send Now")
- Skips jobs with status other than `pending`/`active`

| trigger_type | APScheduler trigger | Notes |
|---|---|---|
| `now` | `DateTrigger` with +2s delay | Fires immediately after creation |
| `date` | `DateTrigger` at parsed datetime | Times are stored in UTC |
| `cron` | `CronTrigger` parsed from expression | Cron expressions are generated by the UI (Daily/Weekdays/Weekly/Monthly/Raw) |

### Execution

When a trigger fires, APScheduler calls `send_job(job_id)`:
1. Loads the job and its token from DB
2. If `skip_count > 0` and not a trigger/now job: decrements skip_count (or marks completed for date-type) and logs "skipped" — no API call
3. Decrypts the token with Fernet
4. Calls `WhatsAppSender.send()` (async HTTP to Whapi.Cloud)
5. Creates a `Log` entry with the result
6. Updates job status (completed for one-time, stays active for cron, remains "trigger" for trigger-type)

## Timezone handling

- User selects a timezone during signup (stored in `users.timezone` and JWT `tz` field)
- "One-time" date input is converted from user's timezone to UTC before storing
- Stored trigger values are always in UTC
- `tz()` Jinja2 filter converts UTC datetimes to the user's timezone for display
- `parse_dt()` converts stored datetime strings to Python datetime objects
- `cron_to_text()` converts cron expressions to human-readable text (e.g. "Daily at 09:00", "Weekdays at 14:30")
- Users can change timezone at any time via `/auth/timezone`
- The nav bar displays the user's timezone as a clickable badge

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
- Image file is deleted when the associated job is deleted (or replaced on edit)
- Sent as `multipart/form-data` to Whapi.Cloud's `/messages/image` endpoint

## Group picker

The job creation form calls `WhatsAppSender.get_groups()` which hits Whapi.Cloud's `GET /groups` endpoint. Groups are displayed as a `<select>` dropdown showing group names. A "Manual entry" option allows typing a group ID directly. Group names are cached in the `group_name` column on the job.

## Scheduler persistence

### Local development

The scheduler runs inside the same process as the web server. Jobs are stored in SQLite and reloaded on every startup.

### Production (Fly.io)

- `auto_stop_machines = false` and `min_machines_running = 1` keep the VM running 24/7
- SQLite database lives on a persistent 1GB volume at `/data`
- The scheduler restarts jobs from the DB if the machine is restarted
- Uploads directory is configurable via `UPLOAD_DIR` env var (defaults to `/data/uploads` on Fly.io)

## UI components

### Dashboard (`/dashboard`)
- Time-based greeting (Good morning/afternoon/evening)
- 6 stat cards with icons: Total Jobs, Active, Sent, Failed, Success Rate, Tokens
- Success rate animated progress bar with gradient + shimmer effect
- Recent Jobs / Recent Activity mini-tables
- Empty state when no data exists

### Jobs list (`/jobs`)
- Status filter tabs: Active, Paused, Completed, Failed, All
- **Search** — text input (`?q=`) that searches label, group_name, and group_id (preserved across filter tabs)
- **Sorting** — clickable column headers (Label, Group, Trigger, Status, Created) with ▲/▼ indicators
- **Pagination** — 25 per page with prev/next, first/last, and numbered page buttons (preserves all query params)
- **Message preview** — click the ▶ button on any row to expand and show the full message text
- **Empty state** — illustrated empty state with icon and "Create your first job" CTA button

### Job actions per row
- **Edit** — navigates to `/jobs/{id}/edit` (available for pending/active/trigger, not "now" jobs)
- **Clone** — copies the job, appends "(copy)" to label, resets status, registers with scheduler
- **Pause** — removes from APScheduler, sets status to "paused"
- **Resume** — re-registers with APScheduler, sets status to "pending"
- **Skip** — increments `skip_count` (next scheduled execution will skip without sending)
- **Skip clear** — resets `skip_count` to 0
- **Send Now** — for `trigger`-type jobs, fires `send_job()` immediately
- **Cancel** — removes from APScheduler, sets status to "cancelled"
- **Delete** — removes from APScheduler and DB, cleans up uploaded image, shows custom confirm modal

### Job form (`/jobs/create`, `/jobs/{id}/edit`)
- Token selector → groups fetched from Whapi.Cloud
- Message text + optional image upload
- Trigger: Send Now / Trigger (on-demand) / One-time (native datetime picker) / Recurring (friendly UI)
- Recurring options: Daily, Weekdays, Weekly (single or custom multi-day), Monthly, Custom cron
- Edit mode pre-fills all fields, including cron frequency detection via `parse_cron_for_form()`
- Image replacement in edit mode deletes old image

### Tokens list (`/tokens`)
- Shows all tokens with name, masked token preview, status badge, last used timestamp
- Toggle active/inactive inline
- Edit token name and/or API key (leave blank to keep current)
- Delete token
- Empty state with "Add your first token" CTA

### Logs (`/logs`)
- Filterable by job ID via `?job_id=` query param
- Expandable response details via `<details>/<summary>`
- Pretty-printed JSON responses
- Timezone-aware timestamps
- Empty state when no logs exist

## Client-side JS features (in `base.html`)

All JS is inline in `base.html` — no build step, no npm.

- **Live clock** — updates every second in the nav bar using `toLocaleTimeString()`
- **Keyboard shortcuts** — `n` navigates to new job, `/` focuses the search input
- **HTMX loading spinner** — counter-based overlay shows/hides on `htmx:beforeRequest` / `htmx:afterRequest`
- **Custom confirm dialog** — styled modal replacing native `confirm()`, supports Escape/click-outside-to-close
- **Toast notifications** — auto-dismissing flash messages parsed from `?success=` / `?error=` query params
- **Message preview toggle** — expandable rows in jobs table via `toggleMsg(id)`
- **Theme toggle** — localStorage-persisted dark/light mode, toggled by a nav button (🌙/☀️)
- **Language toggle** — POST to `/auth/lang` switches between English and Arabic, updates nav direction

## Jinja2 helpers (in `app/main.py`)

These functions are injected as Jinja2 globals:

| Function | Purpose |
|---|---|
| `tz(dt, tz_name)` | Converts UTC datetime to user's timezone, formats as `YYYY-MM-DD HH:MM` |
| `parse_dt(value, fmt)` | Parses a datetime string into a Python datetime object |
| `cron_to_text(expr)` | Converts cron expression to human-readable text ("Daily at 09:00") |
| `_(key)` | Translates a key to the user's language (injected by `render()`) |

The custom `render()` function creates a fresh Jinja2 template environment with `cache_size=0` (workaround for Starlette/Jinja2 compatibility bug with `unhashable` cache keys), and injects the `_()` translation function scoped to the user's language.

## Internationalization (i18n)

Translations live in `app/i18n.py` as a dictionary:
```python
TRANSLATIONS = {
    "en": {},
    "ar": { "Dashboard": "لوحة التحكم", ... }
}
```

The `_()` function does `TRANSLATIONS.get(lang, {}).get(key, key)` — falls back to the English key if no translation exists. Currently supports English and Arabic (RTL). The user's language is stored in `users.lang` and the JWT `lang` field, and also in a `lang` cookie for anonymous pages.

RTL support: when `lang == 'ar'`, the `<html>` tag gets `dir="rtl"`, and CSS rules with `[dir="rtl"]` selectors flip layout (nav, tables, stats, pagination, toasts, etc.).

## Dark mode

CSS custom properties in `app.css` define light and dark themes via `:root` and `[data-theme="dark"]`. Toggling is handled by a nav button that sets `data-theme` on `<html>` and persists to `localStorage`. An inline `<script>` in `base.html` applies the saved theme before rendering to prevent flash-of-wrong-theme.

## Jinja2 compatibility note

Starlette's `Jinja2Templates` has a compatibility bug with Jinja2 3.1.6+ (`unhashable` cache key error). The workaround is to use a raw Jinja2 `Environment` with `cache_size=0` and a custom `render()` function that handles `user_tz` and `_()` injection.

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

## Tests

Located in `tests/test_crypto.py` — 6 pytest tests covering Fernet encryption/decryption round-trip, different outputs on each call (IV), wrong-key rejection, empty strings, long tokens, and special characters.
