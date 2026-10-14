# Built-in WhatsApp

WhatSend uses the pinned WhiskeySockets Baileys client to connect directly to WhatsApp's Web protocol over WebSockets. It runs in the same deployment; no Relay server or browser is required. This is an unofficial integration, not Meta's Cloud API.

## Connect

1. Sign in to WhatSend and open **Tokens → Connect WhatsApp**.
2. Give the connection an optional name and select **Continue to QR code → Get QR code**.
3. On your phone open **WhatsApp → Linked devices → Link a device**, then scan the QR code.
4. Once connected, create a job and select the **WhatsApp QR** connection. Groups load from that account. You can also enter an international phone number (for example `+201234567890`) or group JID ending in `@g.us` manually.

Saved sessions reconnect after a restart. The pairing page offers **Log out of WhatsApp** to unlink and clear credentials. Deleting a connection also logs it out and removes its local session data. QR codes stay in memory and are not logged or cached.

For communities, select the entry labeled **Announcements**. The picker combines WhatsApp’s ordinary group and community lists, and checks linked chats for missing announcement groups. Community subgroups include the community name so similarly named chats are easy to distinguish. Community containers are excluded from the picker; existing jobs or manually entered community IDs resolve to the linked announcement group before sending. If that group cannot be uniquely identified for the connected account, the message fails before transmission with an explanation. The connected account must have permission to post in the announcement group.

## Jobs and delivery

Send Now, dates, recurring schedules, and manual triggers work with built-in connections. Text is limited to 4096 UTF-8 bytes, captions to 1024 bytes. JPEG/PNG/WebP attachments can be up to 16MiB. MP4 and documents can be up to 50MiB. Documents include PDF, Word (DOC/DOCX), Excel (XLS/XLSX), PowerPoint (PPT/PPTX), OpenDocument (ODT/ODS/ODP), TXT, CSV, RTF, and ZIP. Jobs preserve the uploaded filename, including for scheduled and recurring deliveries. Document captions and delivery status follow the same rules as other attachments.

Accepted sends show queued/processing until a WhatsApp server acknowledgement marks them sent. Sent means WhatsApp accepted the message, not that the recipient read it. Interrupted or uncertain sends show unknown and are never automatically resubmitted; check WhatsApp before manually sending again. Pending messages that have not yet been attempted survive restarts. Session keys, pending payloads, and operation records are encrypted on the persistent volume.

## External API

Enable `API_KEYS_ENABLED=true`, create a WhatSend API key linked to your QR connection, and use it as the Bearer credential. The provider is inferred, or can be explicitly set to `native`.

```bash
curl -X POST https://whatsend-app.fly.dev/api/send \
  -H 'Authorization: Bearer YOUR_WHATSEND_KEY' \
  -H 'Idempotency-Key: your-unique-message-key' \
  -H 'Content-Type: application/json' \
  -d '{"number":"+201234567890","message":"Hello","provider":"native"}'
```

Poll the returned status URL `GET /api/operations/{id}` until sent/failed/unknown. Reusing a key with the same payload returns the original operation; changing its payload gives 409. `POST /api/send/media` takes multipart `group_id`, `caption`, and `file` with the same headers. `GET /api/groups` lists groups; `GET /api/connection` reports connection state.

## Deployment and migration

Run Node 22+ and `npm ci` in `whatsapp-worker`, then start `python run.py`. The launcher applies migrations, starts the private loopback worker, and supervises both processes. The Docker image includes both runtimes. Fly stays running for schedules, delivery polling, and WhatsApp reconnects; it has 512MiB RAM. `WHATSAPP_DATA_DIR=/data/whatsapp` stores encrypted sessions. Keep `TOKEN_ENCRYPTION_KEY` (or fallback `SECRET_KEY`) stable and back up the volume. The worker's random internal Bearer secret is generated at each startup and never sent to browsers. When running processes separately, supply `WHATSAPP_WORKER_SECRET` to both and `WHATSAPP_STORAGE_KEY` to Node.

Existing Relay connections become built-in QR connections while retaining their job and WhatSend API-key links. External provider credentials are cleared and every migrated connection needs a fresh QR scan. Pending historical Relay operations become unknown and are not replayed. Internal database fields with legacy Relay names remain for migration compatibility; no runtime code contacts Relay.

## Composer helpers

Open **Saved recipient lists** below the recipient picker to save the current selection. Lists belong to your account and the selected WhatsApp connection. **Add list** merges its recipients into the current selection without duplicating IDs; deleting a saved list leaves the current selection intact.

The composer saves drafts on the current device using browser storage. **Restore draft** recovers the message, label, recipients, timing fields, and selected attachment. Drafts are separate for each signed-in account and each new/edit composer. A successful submission clears that draft. A validation error retains it. **Discard draft** removes the saved copy without changing the current form. If browser storage is unavailable or full, the composer shows a warning instead of claiming the draft was saved. Drafts do not sync across devices.

**Send Now** opens the job details and its live progress panel. It displays the latest run, refreshing while work is pending. Sent means WhatsApp acknowledged acceptance, not that recipients read the message. Failed and uncertain outcomes remain separate; checking progress never resends a message.

## Finite mixed-content plans

In the message composer choose **Finite message plan**, then select a recurring schedule (daily, weekdays, weekly/custom days, monthly, or a cron expression). External triggers can also advance a plan one send at a time. Entries are numbered scheduled occurrences, independent of calendar days or weeks.

Add any combination of sources:

- **Messages and documents:** add batches, write individual messages, and upload several attachments to each batch. A batch can contain just documents. The common message above the builder is included in every scheduled send.
- **PDF page chunks:** upload an unencrypted PDF up to 100MB, choose a 1-based start page and inclusive end page, and set pages per send. A blank end page uses the document’s last page. The final chunk may have fewer pages. Each generated PDF must fit the 50MB attachment limit.
- **YouTube / SoundCloud playlist:** import an accessible playlist and choose 1–20 items per send. Items become titled links in playlist order. The playlist is captured at import time; later additions do not extend the plan. Unavailable items are reported as skipped. Audio/video files are not downloaded.

Choose **Together on each occurrence** to send the next batch from every unfinished source. If sources contain 3, 5, and 2 batches, the plan lasts 5 scheduled sends. Choose **One after another** to finish sources in their displayed order; the same sources last 10 sends. Move sources with the arrow buttons and inspect **Preview scheduled sends** before saving. Plans support up to 20 sources, 1000 scheduled sends, and 20 attachments per occurrence.

Variables are expanded when sending, in the schedule’s timezone:

| Variable | Meaning |
| --- | --- |
| `{send_number}` / `{counter}` | Current scheduled send, starting at 1 |
| `{total_sends}` | Total finite plan length; `∞` for ordinary repeating messages |
| `{today}` / `{date}` | Local date as YYYY-MM-DD |
| `{time}` | Local time as HH:MM |

Text and attachments are separate WhatsApp messages within an occurrence; the progress panel groups their outcomes by recipient. The next occurrence cannot reuse an entry already claimed or still awaiting reconciliation. Pause/resume retains the position. Skipping delays the next batch rather than discarding it. Started plan contents are locked; clone the plan to make a new sequence from Send #1.

After the final occurrence settles, future scheduled runs are removed. The result is Completed, Failed, or Unknown depending on delivery outcomes. Failed and uncertain parts are not resent automatically, including after a restart. Imported attachments that were never assigned to a schedule expire after seven days; attached plan files remain available, including through clones.
