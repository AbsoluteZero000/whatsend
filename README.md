# WhatsApp Scheduler

A Python script that sends WhatsApp messages (text + images) to groups at scheduled times, configured entirely from a YAML file.

---

## Project structure

```
whatsapp_scheduler/
├── scheduler.py       ← entry point; reads config and starts the scheduler
├── sender.py          ← Green API wrapper (text + image sending)
├── config.yaml        ← all your groups, messages, and schedules live here
├── requirements.txt
└── assets/            ← put your image files here
```

---

## Setup

### 1. Get a Green API account

1. Go to [https://green-api.com](https://green-api.com) and sign up (free tier available).
2. Create a new **instance**.
3. Scan the QR code with the WhatsApp account that will send the messages.
4. Copy your **Instance ID** and **API Token** from the dashboard.

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Fill in config.yaml

```yaml
api:
  instance_id: "7105xxxxxx"
  api_token:   "your_token_here"
```

Add your scheduled messages under `schedule:` — see the examples already in the file.

### 4. Find your group IDs

Group IDs look like `120363xxxxxxxxxxxxxxxxxx@g.us`.

**Easiest way — use the Green API console:**
```
GET https://api.green-api.com/waInstance{id}/getChats/{token}
```
This returns all chats with their IDs. Look for the group name.

### 5. Run the scheduler

```bash
python scheduler.py
```

The script stays running and fires messages at the scheduled times.  
All activity is logged to `scheduler.log` and to stdout.

---

## Scheduling syntax

### One-time message (`date`)

```yaml
date: "2026-06-15 09:00"   # UTC — YYYY-MM-DD HH:MM
```

### Recurring message (`cron`)

```yaml
cron: "MIN HOUR DOM MON DOW"
```

| Example              | Meaning                     |
|----------------------|-----------------------------|
| `0 9 * * 1`          | Every Monday at 09:00       |
| `0 10 1 * *`         | 1st of every month at 10:00 |
| `30 8 * * 1-5`       | Weekdays at 08:30           |
| `0 12 * * 0`         | Every Sunday at noon        |

All times are **UTC** by default. Change `timezone="UTC"` in `scheduler.py` if needed.

---

## Adding images

Put image files in the `assets/` folder and reference them in the config:

```yaml
image: "assets/my_banner.jpg"   # JPEG, PNG, GIF, WebP supported
```

If `image` is omitted or the file is missing, only the text message is sent.

---

## Running as a background service (optional)

**Linux/macOS — using `screen`:**
```bash
screen -S whatsapp
python scheduler.py
# Ctrl+A then D to detach
```

**Or create a systemd service** for auto-start on boot.
