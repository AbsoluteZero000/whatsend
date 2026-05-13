# WhatSend — WhatsApp Scheduler

## The journey

I started with a simple Python script (`scheduler.py`) that reads a YAML file and fires off WhatsApp messages via the Whapi.Cloud API at scheduled times. It worked, but editing YAML by hand got old fast.

So I thought: "I'll wrap this in a REST API so people can hit endpoints to manage their messages." That's better, but still not friendly — who wants to curl endpoints to schedule a message?

So I built a **dashboard**. Now anyone can sign up, add their Whapi.Cloud tokens, create scheduled messages, and watch the logs — all from a browser.

## Stack

| Piece | What |
|---|---|
| Backend | **FastAPI** (async Python) |
| Database | **SQLite** via **SQLAlchemy 2.0** (async) |
| Frontend | **Jinja2** templates + **HTMX** (no JS build step) |
| Auth | JWT in httpOnly cookies, bcrypt hashing |
| Scheduler | **APScheduler** (kept from the original script) |

## Schema

```
users ──1:N── tokens ──1:N── jobs ──1:N── logs
```

- A **user** has many API tokens and many jobs
- A **token** belongs to a user and is used by many jobs
- A **job** belongs to a user and a token; can be one-time (`date`) or recurring (`cron`)
- A **log** records every send attempt (success or failure) for a job

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python run.py
```

Open http://localhost:8000, sign up, add a Whapi.Cloud token, and create your first scheduled message.

## Directory layout

```
whatsend/
├── app/
│   ├── main.py              # FastAPI app
│   ├── config.py            # Settings
│   ├── database.py          # SQLAlchemy async engine + session
│   ├── models/              # User, Token, Job, Log
│   ├── schemas/             # Pydantic models
│   ├── routers/             # auth, dashboard, tokens, jobs, logs
│   ├── services/            # auth (JWT/bcrypt), sender (Whapi.Cloud)
│   ├── templates/           # Jinja2
│   └── static/              # CSS
├── run.py                   # uvicorn entry point
├── scheduler.py             # Original CLI script (still works)
└── sender.py                # Original sender (still works)
```
