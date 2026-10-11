# Archived

Relay was replaced by [built-in WhatsApp QR connections](whatsapp.md) on 2026-10-11. The original plan below is kept as implementation history.

# Relay provider implementation plan

Reviewed local WhatSend and `../relay` source on 2026-10-10. This is a source-level endpoint review; no running Relay service was tested and no messages were sent.

## Scope and recommended defaults

Add `relay` alongside the existing `whatsapp` (Whapi.Cloud) provider. Each user connection stores its backend URL entered in the site, encrypted Relay API key, and session UUID. `RELAY_BASE_URL` remains a legacy fallback. Initially users pair their session in Relay and paste its URL/key/session into WhatSend. In-app QR pairing is a follow-up.

Keep existing Whapi connections, jobs, and API callers working. Resolve the provider from the selected connection, rather than adding an independently editable provider to jobs.

## Verified Relay contract

All application paths below start with `/api/v1`. Protected routes accept `Authorization: Bearer <credential>`; use an API key for the integration rather than an expiring login JWT.

| Operation | Endpoint | Scope / payload / result |
| --- | --- | --- |
| Register/login | `POST /auth/register`, `POST /auth/login` | JSON `email`, `password`; setup only |
| Issue key | `POST /api-keys` | `api_keys:write`; JSON `name`, `scopes`, optional `expires_at`; key returned once |
| Create session | `POST /sessions` | `sessions:write` |
| Connect/disconnect | `POST /sessions/{session_id}/connect`, `/disconnect` | `sessions:write` |
| Check connection | `GET /sessions/{session_id}/status` | `sessions:read`; `data.state` |
| Pair | `GET /sessions/{session_id}/qr` | `sessions:read`; `data.value`, `png_base64`, `expires_at` |
| Group picker | `GET /sessions/{session_id}/groups` | `chats:read`; `page_size` up to 200, `cursor`; response `data`, `next_cursor` |
| Enqueue text | `POST /sessions/{session_id}/messages` | `messages:write`; JSON `recipient`, `text`; required `Idempotency-Key` header |
| Enqueue attachment | Same message endpoint | Multipart `recipient`, `caption`, **`file`**; required `Idempotency-Key` |
| Check delivery | `GET /sessions/{session_id}/messages/{message_id}` | `messages:read`; response `data.status`, `error_code`, `external_message_id`, `attempt_count` |
| List messages | `GET /sessions/{session_id}/messages` | `messages:read`; cursor pagination; no idempotency-key filter |
| Directory extensions | `GET /sessions/{session_id}/contacts`, `/conversations` | `chats:read`; optional future direct-recipient picker |

Minimum initial key scopes: `sessions:read`, `chats:read`, `messages:write`, `messages:read`. Add `sessions:write` only for in-app session lifecycle operations.

Important mappings and limits:

- Sends return HTTP **202**, `{"data":{"id":"<uuid>","status":"pending"}}`. This acknowledges enqueueing, not sending.
- Current outbound delivery states are `pending`, `processing`, `sent`, `failed`. Relay retries delivery internally, up to five attempts. `sent` does not establish recipient delivery or read status.
- Group `id` is Relay's internal UUID. WhatSend must use **`external_chat_id`** as its recipient/group ID, and `name` as the display label. Follow every `next_cursor`.
- Supported attachments: JPEG, PNG, WebP (16 MiB maximum), MP4 (64 MiB maximum). WhatSend's 50 MiB upload limit still applies. Relay currently rejects PDF/documents, GIF, and other video formats.
- Text is limited to 4096 UTF-8 bytes, captions to 1024 bytes, recipient to 128 bytes. Validate bytes rather than Python character counts.
- Errors use `{"error":{"code":"...","message":"..."}}`. Distinguish `session_unavailable` and `idempotency_conflict`, both HTTP 409.
- Public WebSocket/webhook delivery routes are not registered in the current backend. Use REST polling for this implementation.

Source anchors: `../relay/backend/cmd/relay-api/main.go`, `internal/messaging/transport/http/handler.go`, `internal/messaging/application/service.go`, `internal/messaging/application/dispatcher.go`, `internal/messaging/domain/media.go`, and `internal/sessions/transport/http/handler.go` under the Relay backend.

## Implementation sequence

### 1. Make ambiguous submissions recoverable in Relay

Relay currently enforces a unique `(session_id, idempotency_key)` and returns 409 on duplicates. It does not return the original ID, expose a lookup by key, or include the key in message responses. A timeout after acceptance therefore cannot be reconciled safely through its current API.

Recommended prerequisite: add an ownership-scoped lookup by idempotency key, or make identical POST replays return the original message ID/current status. If implementing replay, compare the request payload and reject changed payloads with a conflict; account for media content hashes and clean up temporary uploads. Add Relay contract tests for identical replay, changed payload, session/user isolation, and accepted-request/lost-response recovery. Do not infer the original message from matching text and timestamps.

Without this change, WhatSend can expose an ambiguous submission as `unknown` and require manual resolution, but must not create a new key and resend automatically.

### 2. Add provider-aware connections

- Alembic migration: `tokens.provider` default/server default `whatsapp`; nullable `tokens.relay_session_id`. Backfill existing rows as Whapi.
- Keep API keys encrypted with the existing Fernet service. Require a valid session UUID for Relay and validate the provider server-side.
- Add the per-connection Relay backend URL to the site form. Validate an HTTP/HTTPS origin without credentials, path, query or fragment, and retain `RELAY_BASE_URL` as a legacy fallback. Persist the connection URL with each operation so reconciliation cannot switch backends.
- Update token create/edit/list templates and routes with provider labels, Relay session field, and a connection check. Retain the current blank-key-on-edit behavior; avoid changing a connection's provider/session while jobs or pending sends reference it.
- Add English/Arabic labels in `app/i18n.py`.

### 3. Implement the adapter and connection-specific groups

- Add `app/services/relay_sender.py` with text/media enqueue, message lookup, status check, pagination, nested error decoding, and provider capabilities.
- Extend `app/services/sender.py` with a common sender/result contract and a factory accepting the connection's provider/session metadata. Keep Whapi's current send behavior.
- Replace direct `WhatsAppSender` construction in the scheduler and all three job group-loading paths.
- Add an account-scoped groups endpoint for a selected token; refresh the picker when that token changes. Create/edit must fetch from the selected connection, and group-name backfills must use each job's own connection. The current code uses the first active token with groups, which can display the wrong account's groups.
- Preserve existing selected recipients during edit; when the connection changes, clear/revalidate recipients with an explicit UI indication. Validate provider attachment capabilities and byte limits on the server as well as in the form.

### 4. Persist enqueue and delivery separately

- Before network submission, commit a per-recipient operation with a stable idempotency key and a durable logical run ID. Reuse that key across submission retries/restarts; each new recurring occurrence or explicit new send receives a new operation/key. The current scheduler creates a fresh `run_id` on every retry, so it cannot serve as a durable key unchanged.
- Extend delivery persistence (a dedicated operation table or additional delivery-attempt fields) with provider, connection/session snapshot, idempotency key, upstream message ID, submission/delivery state, and next reconciliation time. Keep retry history distinct from the logical operation.
- Store `queued` on a 202 response. A durable scheduler reconciliation task polls accepted messages and recovers unfinished operations at startup. Commit state before and after network calls rather than holding an uncommitted database write across a long wait.
- Mark sent/completed only after Relay reports `sent`. Leave recurring jobs active while tracking each occurrence independently. Allow partial outcomes across groups without resending successful recipients.
- Let Relay own retries after acceptance. Poll failures and polling deadlines must not trigger another enqueue. Report terminal Relay failures; a deliberate retry after terminal failure creates a new operation.
- Retry transient submission failures with the same key. Treat authentication, missing scope, invalid payload, and unsupported media as actionable failures; handle unavailable sessions separately. Preserve unresolved operations when a connection is disabled or deleted so they are not silently reported as sent or retried through another connection.
- Update job/log detail, list filters, dashboard/admin counts, and flash messages to distinguish queued, sent, failed, and unknown. The current Send Now flash always says "Job sent".

### 5. Route the optional external API through its linked connection

- Update `app/routers/api.py`: `get_linked_token` currently rejects every provider except `whatsapp`; `/api/groups` also hardcodes Whapi.
- Infer the provider from the API key's linked token. Make the request provider optional; validate explicit values against that token. Existing requests linked to Whapi retain their behavior.
- For Relay `/api/send`, return 202 with a queued state and a WhatSend operation ID; provide an account/API-key-scoped status route to retrieve its eventual outcome. Route through the same durable operation service as scheduled sends.
- Support caller idempotency for external sends so a repeated client request resolves to the same operation. Use a locally scoped key mapped to Relay's key; keep repeated calls isolated by account/API key.
- Replace Whapi-specific error strings with provider-aware errors and avoid exposing credentials in logs or responses.

### 6. Verify and document

Meaningful automated coverage:

- Migration preserves old connections and existing Whapi send/API behavior.
- Relay request headers, text/multipart fields, response envelopes, limits, and all-page group mapping.
- Connection ownership, mismatched requested providers, disabled keys/tokens, and selected-token group isolation.
- Queued → processing → sent/failed; restart recovery; multi-group partial delivery; recurring occurrences; accepted request with lost response; no duplicate enqueue after poll failure.
- Stable keys across retries, new keys for new occurrences, and concurrent reconciliation/submission claims.
- English/Arabic forms, connection switching, and media rejection before scheduling.

Then verify against a running Relay test account: connection check, group picker, one text and supported attachment, eventual status, and restart recovery. Actual message sends require explicitly authorized test recipients. Update README and environment examples with setup/scopes and supported attachments.

## Delivery order

Implement the Relay idempotency recovery contract first, then connection schema/factory, text/groups adapter, durable delivery reconciliation, media/UI, and optional external API support. Initial setup uses an already-paired session. In-app QR pairing, contacts/conversation pickers, and document support are separate follow-ups.
