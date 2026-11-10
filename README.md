# EchoSend — Automated & Scheduled Messaging

Schedule and send WhatsApp messages from a browser dashboard, with built-in WhatsApp QR pairing and optional [Whapi.Cloud](https://whapi.cloud) tokens.

## The story

This project was born out of a simple problem: we had a place where WhatsApp messages needed to be sent for events on specific days, but we'd often forget or be late.

**v1** was a Python script using `pywhatkit` to send messages automatically. It worked — kinda. But it required a browser with an already signed-in WhatsApp user, was painfully slow, and couldn't run on a server.

We went back to manual sending. Then **v2** appeared: same idea but powered by the Whapi.Cloud API — no browser needed, fast, server-ready. Jobs were configured via a YAML file, which meant editing files and redeploying every time something changed.

**v3** added REST APIs so everything could be configured remotely. Then came the question: how will normal people use this? That's when the web dashboard was born — Jinja2 templates, vanilla JS, zero build step.

And that's where we are today. EchoSend is a full-featured WhatsApp scheduler you can deploy on a $5/month server and control from any browser.

## Features

- **Send Now** — Fire off a message immediately with a single click
- **Trigger on-demand** — Create a job that sits ready and fires when you hit "Send Now"
- **One-time scheduling** — Pick a date & time with a native datetime picker
- **Recurring scheduling** — User-friendly UI (Daily, Weekdays, Weekly with multi-day checkboxes, Monthly, Custom cron) — no cron syntax needed
- **Finite mixed-content plans** — Build colored lines of messages, documents, PDF chunks or custom page ranges, and YouTube/SoundCloud links. Run lines together while each line finishes one book or playlist before starting the next; preview every send in a table and stop automatically when all content finishes
- **Changing text** — Put `{word}` (and `{word2}`…`{word5}` for more parts) in a message and give each a list of words, sentences, numbers, or ranges like `1-5`; each send uses the next item of every list, shorter lists start over on their own, and when the longest ends it starts again or stops. A *Fill with numbers* helper builds `1, 2, 3…` or ranges for you
- **Message variables** — Insert `{send_number}`, `{counter}`, `{total_sends}`, `{today}`, `{date}`, and `{time}` using the schedule’s timezone
- **Saved recipient lists** — Save selected recipients under a name and reuse them with the same WhatsApp connection
- **Autosaved drafts** — Restore message fields, selected recipients, schedules, and attachments on the same device; drafts clear after a successful submission
- **Live sending progress** — Send Now opens a progress page with automatically updated per-recipient status and counts for sent, pending, failed, and uncertain attempts
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

Install the WhatsApp worker dependencies with `cd whatsapp-worker && npm ci`, then start both services with `python run.py` (Node 22+ required). Open http://localhost:8000, sign up, choose **Tokens → Connect WhatsApp**, scan the QR code from Linked devices on your phone, and create your first message. Optional Whapi.Cloud tokens remain supported.

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

Built-in **WhatsApp QR** connections support personal/group text messages, JPEG/PNG/WebP images, MP4 video, group lookup, delivery status, and dashboard jobs. Choose **Tokens → Connect WhatsApp → Get QR code**, scan it on your phone, and link a WhatSend API key to that connection if you need the external API. No Relay service, provider registration, backend URL, or provider token is needed. Native sends require an `Idempotency-Key` and return **202 queued** until WhatsApp acknowledges them. See [WhatsApp setup and API examples](docs/whatsapp.md).

## Claude connector (optional)

Set `CLAUDE_CONNECTOR_ENABLED=true` to let users connect Claude (claude.ai, the Claude apps, Claude Desktop/Code) to their account. It is off by default. When it is on, **Settings** shows the connector address (`https://your-app.example.com/mcp`) and the apps a user has connected, each with a **Disconnect** button.

In Claude, open **Settings → Connectors → Add custom connector** and paste the address. Claude registers itself, sends the user to EchoSend to sign in and press **Allow**, and can then:

- read the account timezone, WhatsApp connections, groups and saved recipient lists;
- list messages and read one message's schedule and delivery status;
- create text messages to send now, once, repeating, or on specific dates;
- pause, resume and cancel messages.

It cannot delete messages, upload attachments, set up message series, or change settings. Administrator accounts can't connect apps.

How it works: `/mcp` is a stateless MCP server (Streamable HTTP, JSON responses). Sign-in is OAuth 2.1 with dynamic client registration, PKCE (S256), and resource indicators. Discovery lives at `/.well-known/oauth-protected-resource` and `/.well-known/oauth-authorization-server`. Access tokens last one hour and refresh tokens rotate on every use. Only SHA-256 hashes of codes and tokens are stored. Set `PUBLIC_URL` if the app sits behind a proxy that changes its public address.

## Facebook and Instagram posts (optional)

EchoSend can schedule posts to Facebook Pages and Instagram accounts, using the same scheduling, repeats, retries and history as WhatsApp messages. The two connect separately on the Connections page:

- **Connect Facebook** signs in with Facebook. Facebook asks which Pages to share, and EchoSend adds one connection per shared Page. Personal Facebook profiles can't be posted to; Meta removed that for all apps in 2018.
- **Connect Instagram** signs in with Instagram itself (Instagram Login), so no Facebook Page or Facebook account is needed. The account must be a Creator or Business account; switching a personal account to Creator is free (Instagram Settings → Account type and tools).

When you write a message, pick a Page or Instagram connection: the text becomes the post (or caption) and the attachment becomes its photo or video.

Setup (one Meta developer app covers both):

1. At [developers.facebook.com](https://developers.facebook.com/apps), create an app (type **Business**; no registered business or verification is needed for your own accounts).
2. **Facebook:** add the **Facebook Login for Business** product and add `https://<your-site>/tokens/meta/callback` as a valid OAuth redirect URI. Use the app ID and secret from App settings → Basic as `META_APP_ID` and `META_APP_SECRET`.
3. **Instagram:** add the **Instagram** product, choose **API setup with Instagram login**, and add `https://<your-site>/tokens/instagram/callback` as the redirect URL under business login settings. That page shows a separate **Instagram app ID** and **Instagram app secret**: use them as `INSTAGRAM_APP_ID` and `INSTAGRAM_APP_SECRET`.
4. Set `PUBLIC_URL` as well, for example on Fly: `fly secrets set META_APP_ID=... META_APP_SECRET=... INSTAGRAM_APP_ID=... INSTAGRAM_APP_SECRET=... PUBLIC_URL=https://echosend.fly.dev -a echosend`. Each button appears only when its credentials are set.
5. While the app is in Development mode, only accounts with a role on the app can connect (add your Instagram account as an Instagram tester under App roles). To let anyone connect, request Advanced Access through Meta App Review: `pages_show_list`, `pages_read_engagement`, `pages_manage_posts` and `business_management` for Facebook; `instagram_business_basic` and `instagram_business_content_publish` for Instagram.

Limits:

- **Facebook:** text, one photo (JPEG, PNG or GIF, up to 10MB) or one video per post.
- **Instagram:** every post needs one JPEG photo (up to 8MB) or an MP4/MOV video, which is posted as a Reel. Captions can be up to 2,200 characters and 30 hashtags. Instagram downloads the file from a signed link that expires after an hour, so `PUBLIC_URL` must be reachable from the internet.
- Facebook Page logins don't expire. They stop working if you remove the app in Facebook settings, lose your role on the Page, or change your Facebook password. Instagram logins last 60 days; EchoSend renews them automatically once a day when fewer than 20 days are left. In either case, if posting fails with a login error, connect again: that refreshes the existing connections.
- If Meta accepts a post but the response is lost (for example, a timeout), the automatic retry can post it twice.

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
│   ├── services/             # auth (JWT/bcrypt), crypto (Fernet), sender (built-in WhatsApp/Whapi.Cloud), scheduler (APScheduler)
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

## Adding providers

See [Provider extensibility and contributor guide](docs/providers.md) for the assessment,
base sender contract, independent provider packages, and current extension limits.
