# Built-in WhatsApp

WhatSend uses the pinned WhiskeySockets Baileys client to connect directly to WhatsApp's Web protocol over WebSockets. It runs in the same deployment; no Relay server or browser is required. This is an unofficial integration, not Meta's Cloud API.

## Connect

1. Sign in to WhatSend and open **Tokens → Connect WhatsApp**.
2. Give the connection an optional name and select **Continue to QR code → Get QR code**.
3. On your phone open **WhatsApp → Linked devices → Link a device**, then scan the QR code.
4. Once connected, create a job and select the **WhatsApp QR** connection. Groups load from that account. You can also enter an international phone number (for example `+201234567890`) or group JID ending in `@g.us` manually.

Saved sessions reconnect after a restart. The pairing page offers **Log out of WhatsApp** to unlink and clear credentials. Deleting a connection also logs it out and removes its local session data. QR codes stay in memory and are not logged or cached.

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
