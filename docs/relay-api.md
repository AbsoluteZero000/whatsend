# Relay API provider

Relay is supported by WhatSend's optional external API. Scheduled/dashboard jobs continue to use Whapi. WhatSend can create a Relay session and display its QR code for pairing directly in the site.

## Setup

1. Install this revision and run `alembic upgrade head` against the WhatSend database before starting the app. Existing tokens default to `whatsapp`.
2. Set `API_KEYS_ENABLED=true` and restart WhatSend (enabled in the Fly configuration). The Relay URL is entered in the site, independently for each connection. It must be reachable from WhatSend's deployment; container `127.0.0.1` refers to the WhatSend container itself.
3. In WhatSend, open **Tokens → Connect Relay**, enter the Relay backend URL, and register with your email and a password of at least 12 characters. Existing Relay users can choose **Log in to an existing account**. WhatSend creates an API key with `sessions:read`, `sessions:write`, `chats:read`, `messages:read`, `messages:write` scopes, stores it encrypted, and takes you to QR pairing. Your password and temporary login token are not saved. You can also add an existing API key through **Add Token**. `sessions:write` is needed to create/connect sessions.
4. In WhatSend's Tokens page, add a token with provider **Relay**, its backend origin (such as `https://relay.example.com`, without `/api/v1`), and the Relay API key. Leave the session ID blank to create one, or enter an existing session UUID. Click **Save and pair**, then **Get QR code**. On your phone open WhatsApp → Linked devices → Link a device and scan the displayed QR. WhatSend automatically refreshes the code and checks until it reports connected. Existing tokens have a **Pair WhatsApp** action for reconnecting.
5. Generate a WhatSend API key linked to that Relay token. Send the **WhatSend** key as your bearer credential on every request below.

The Relay key remains encrypted at rest. To change provider/session/backend URL, create a new connection; replacing its credential is allowed. Origins must use HTTP or HTTPS without embedded credentials, query strings or fragments. `RELAY_BASE_URL` remains a fallback for legacy connections that lack a stored URL. Relay currently allows one active session per account: if session creation reports a conflict, enter your existing session ID. Session creation is not retried automatically after a timeout.

Pairing uses authenticated, account-scoped token routes and CSRF-protected connect requests. Only the PNG and expiry are sent to the pairing page; the raw pairing value is not returned. QR responses are not cached, written to the database, or added to delivery logs. Expired codes are hidden and refreshed. Connecting the WhatsApp account requires you to scan the code on your phone; WhatSend does not scan it for you.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/connection` | Linked provider/token metadata and Relay's connection state |
| GET | `/api/groups` | All group pages, normalized to `{id, name}` using WhatsApp external chat IDs |
| POST | `/api/send` | Text submission |
| POST | `/api/send/media` | Relay attachment submission, multipart fields `group_id`, `caption` (optional), `file` |
| GET | `/api/operations/{operation_id}` | Poll delivery for a submission made using this WhatSend API key |

For Relay sends, provide an `Idempotency-Key` header containing 1–128 printable ASCII characters with no leading/trailing whitespace. Reuse it for retries of the same payload; use a new key for a genuinely new message. Keys are isolated by WhatSend API credential. Changed payloads/connections using the same key return HTTP 409. Never switch keys merely because a request timed out.

Example text request:

```bash
curl https://whatsend.example.com/api/send \
  -H 'Authorization: Bearer wts_1_yourapikey' \
  -H 'Idempotency-Key: reminder-2026-10-10-main' \
  -H 'Content-Type: application/json' \
  -d '{"group_id":"120363123456789@g.us","message":"Hello group"}'
```

Provider is inferred from the linked token. An explicit `provider` must match it. The existing `number` alias remains supported. Whapi text requests keep their current response and do not require an idempotency header.

Example Relay acknowledgement (HTTP 202):

```json
{
  "success": true,
  "provider": "relay",
  "group_id": "120363123456789@g.us",
  "operation_id": "c2ba2230-7565-4228-9444-082e4b111d82",
  "status": "queued",
  "data": {"id":"8ad65e56-724d-4cc1-9378-96b5afeb4a1b","status":"pending"},
  "status_url": "/api/operations/c2ba2230-7565-4228-9444-082e4b111d82"
}
```

Poll `status_url` with the same bearer key. A successful status lookup returns HTTP 200 and updates persisted state from Relay. There is no background delivery poller in this API implementation. Polling failures return 502 and preserve the accepted message; retry the status lookup without resubmitting the send. Terminal statuses are cached. Disabling/removing/changing the original connection prevents further upstream polling and returns 409; persisted terminal results remain accessible with the original active WhatSend key.

| Status | Meaning |
| --- | --- |
| `submitting` | The durable operation was created and a submission may still be in progress |
| `queued` | Relay accepted the message; delivery is pending |
| `processing` | Relay is attempting delivery |
| `sent` | Relay reports sent; this is not a recipient read/delivery receipt |
| `failed` | Relay definitively rejected the submission or reports a terminal delivery failure |
| `unknown` | A response was lost/invalid, or submission was interrupted; manual reconciliation is required |

`success` means submission accepted/in progress, or sent; check `status` to know whether sending finished. It is false for `failed`/`unknown`. New/duplicate POSTs return 202 for unfinished/unknown operations, 200 for already-sent operations, or 502 for failed operations. Status GETs return 200 for all known operation states; a different key/account receives 404.

Durable operations survive restarts. WhatSend commits the operation before contacting Relay and never automatically enqueues it again. A stale `submitting` operation is presented as `unknown` after 60 seconds. Relay's current duplicate-key response does not include the original message ID, and its API has no lookup by idempotency key. Therefore unknown outcomes cannot yet be recovered automatically. Check Relay/WhatsApp before intentionally sending a new operation; that may duplicate an already accepted message. The proposed Relay recovery contract is described in [the provider plan](relay-provider-plan.md).

## Attachments and validation

```bash
curl https://whatsend.example.com/api/send/media \
  -H 'Authorization: Bearer wts_1_yourapikey' \
  -H 'Idempotency-Key: image-2026-10-10-main' \
  -F 'group_id=120363123456789@g.us' \
  -F 'caption=Hello group' \
  -F 'file=@photo.png'
```

Supported: JPEG, PNG, WebP up to 16 MiB; MP4 up to WhatSend's 50 MiB limit. Documents, GIF, and other video formats are rejected. Uploads receive extension/content-type/signature validation and temporary local files are removed after submission/replay. Media idempotency includes recipient, caption, original filename, and content hash. Text is limited to 4096 UTF-8 bytes, captions to 1024 bytes, recipients to 128 bytes, and original filenames to 255 bytes. Invalid payloads are rejected before contacting Relay.

Tests use mock HTTP responses and temporary databases; no live WhatsApp sends were performed.
