# Provider extensibility and contributor guide

## Assessment

The app already had useful shared building blocks: connection ownership, encrypted
credentials, recipient lists, scheduling, series, uploads and delivery history.
`Token.provider` was a string rather than a database enum, so adding an integration
does not inherently require a schema migration.

The extension boundary was incomplete:

- Scheduler and series delivery constructed `WhatsAppSender` directly; another
  provider could accidentally send through Whapi or be rejected by series forms.
- Sender selection, API validation, token forms and labels used separate lists or
  conditionals. Contributors would have to edit the same core files.
- Media, recipient discovery, pairing and delivery confirmation were treated as
  if every integration behaved like WhatsApp.
- Recipient name repair searched connections without retaining their identity;
  identical recipient IDs on different accounts could get the wrong name.

The foundation now provides package discovery, a sender base class, immutable
provider definitions, capability checks and a shared factory. Token forms, API
text sends, repeated jobs and series use these definitions. Recipient name repair
is scoped to the job's connection. Existing provider IDs, encrypted credentials,
HTTP response formats and database columns are retained; no migration is needed.

## Add a direct messaging provider

Create your own directory; no central registration edit is required:

```text
app/providers/your_provider/
    __init__.py
    provider.py
    sender.py
tests/providers/your_provider/  # suggested location for new provider tests
```

The package directory and provider ID must match. IDs have at most 20 characters,
start with a lowercase letter and contain lowercase letters, digits or underscores.
The application discovers packages in sorted order once per process. Restart after
adding or removing a package. Invalid definitions, duplicate IDs and import errors
fail loading rather than silently choosing a different provider. Packages are
trusted application code; discovery is not a sandbox for uploaded plugins.

`sender.py`:

```python
from app.providers import BaseSender


class YourSender(BaseSender):
    def __init__(self, token: str):
        self.token = token

    def validate_payload(self, recipient, message, image_path=None):
        super().validate_payload(recipient, message, image_path)
        # Check your provider's recipient, text, media and account limits here.

    async def send(self, chat_id, message, image_path=None, filename=None):
        # Implement your provider HTTP request here; return a JSON-serializable dict.
        # Raise on failure. Never return a queued result as confirmed direct delivery.
        raise NotImplementedError
```

`provider.py`:

```python
from app.providers import Provider
from .sender import YourSender

provider = Provider(
    id="your_provider",
    label="Your provider",
    create_sender=lambda context: YourSender(context.api_token),
    supports_media=False,
    supports_groups=False,
)
```

This example is deliberately incomplete and is not shipped as an enabled provider.
Installed providers: Whapi (`whatsapp`), built-in WhatsApp (`native`), and the
posting providers `facebook` (Facebook Page) and `instagram` (Instagram professional
account), described below.

For token authentication, the provider appears automatically on `/tokens/create`.
Credentials are encrypted by the app and decrypted before creating the sender.
Treat `ProviderContext.api_token` as secret. Constructors and validation must not
send messages or contact upstream services. Do not log tokens or include them in
exceptions. The context does not expose request objects, database sessions or users.

Recipient IDs are opaque strings. The legacy `group_id`, `number`, `image_path`
and `get_groups` names remain for compatibility; they do not require WhatsApp IDs,
phone numbers or image-only attachments. Override `get_groups()` and set
`supports_groups=True` for discovery, returning `[{"id": "...", "name": "..."}]`.
Otherwise the app returns an empty list and manual recipient entry remains usable.
Set `supports_media=True` only when `send` handles attachments and `validate_payload`
checks supported formats, sizes and captions. Preserve `filename` when sending files.

## Delivery semantics

Direct adapters derive from `BaseSender`. A successful `send` returns upstream
acceptance, recorded as `sent` under the existing direct-delivery convention; it
is not proof that a person received or read the message. Standard jobs retry
failed recipients using the existing persisted exponential retry policy. A timeout
after upstream acceptance can cause a duplicate when retried. Use providers with
appropriate idempotency support when that matters; this contract does not supply
an idempotency key to direct adapters. Series deliberately record uncertain outcomes
without automatically resending a part. Account for those policies in adapter tests.

Built-in WhatsApp uses durable enqueueing, acknowledgement IDs and polling, including
unknown-outcome recovery. Its `delivery="whatsapp_worker"` mode is reserved for
`native`. New providers cannot opt into its WhatsApp session tables or retry path.
A future queued provider needs a separate, reviewed persistence contract with
provider-neutral operation IDs, idempotency, status and reconciliation hooks. Do
not implement queues behind a direct adapter or infer delivery from HTTP 202.

## Working independently

- Each integration owner changes `app/providers/<id>/` and its own tests. Use
  relative imports within that package. Never import another provider's sender.
- Shared contracts (`base.py`, `registry.py`), scheduling and auth remain core-owned.
  Agree on contract changes first; preserve existing adapter behavior and run the
  complete suite. Additive optional behavior is preferable to changing signatures.
- Keep provider-specific payloads, HTTP clients, limits and errors inside the
  provider package. The current WhatsApp transports live in their packages;
  `app/services/sender.py` and `native_sender.py` retain compatibility imports.
- Use stable IDs permanently: stored connections and jobs reference them. Before
  removing a provider, retire its jobs and connections. Missing providers fail
  delivery; they never fall back to WhatsApp.
- Use separate provider test folders to avoid shared test-file conflicts. Test
  HTTP payloads with mocked transports, limits, media rejection, discovery,
  authentication failures and ambiguous outcomes. Never require live credentials.
- Schema migrations, dependency manifests and core UI changes still need team
  coordination. Choose a distinct migration revision and reconcile Alembic heads
  when branches merge; package discovery cannot remove those shared boundaries.

Run `.venv/bin/pytest -q` from the repository root. `tests/test_providers.py`
proves discovery of two independent packages and exercises a test-only adapter
through encrypted token creation, API text sends, repeated jobs and message series,
while rejecting any attempt to contact the WhatsApp worker.

## Posting providers: Facebook and Instagram

`facebook` and `instagram` publish posts rather than messages, through the same
`BaseSender` contract. Each connection posts as exactly one account, so its only
recipient is that Page or Instagram account ID; `get_groups()` returns it and
`validate_payload` rejects anything else. A send publishes one post: text, photo or
video on Facebook; a JPEG photo or a Reel on Instagram, with the message as caption.

- Both use `auth="managed"` and are connected independently:
  `app/routers/meta_connect.py` (Facebook Login, `/tokens/meta/...`) adds Pages, and
  `app/routers/instagram_connect.py` (Instagram Login, `/tokens/instagram/...`) adds
  Creator or Business accounts through graph.instagram.com, with no Facebook Page.
  Shared sign-in helpers live in `app/services/social_connections.py`.
- Credentials are stored as encrypted JSON in `Token.api_token`: the access token,
  the account ID and, for Instagram, the expiry (`{"t", "id", "exp"}`). Reconnecting
  refreshes existing connections by account ID. No schema migration was needed.
- Facebook Page tokens don't expire. Instagram tokens last 60 days;
  `app/services/instagram_tokens.py` renews them from a daily scheduler job.
- Shared Graph API code lives in `app/providers/_meta/`. The underscore keeps
  discovery from loading it as a provider. Tokens go in the `Authorization`
  header, never in URLs, and `appsecret_proof` is sent when `META_APP_SECRET` is set.
- Instagram fetches media by URL. `app/services/public_media.py` signs links
  (served by `/media/public/...`) that expire after an hour; it requires `PUBLIC_URL`.
- Posts are not idempotent: a retry after an ambiguous timeout can publish twice.

## Remaining extension work

The following require explicit core extensions when a provider needs them:

- OAuth redirects, refresh tokens, account/page selection, scopes and revocation.
  The shared form currently accepts one encrypted token string, not a structured
  OAuth credential store. `auth="managed"` hides a provider from that form; only
  native WhatsApp currently has a complete managed connection lifecycle.
- Provider-owned callbacks and webhooks, signature checks and inbound message models.
  The existing Meta spike remains separate and unregistered, as before.
- Queued delivery and provider-specific retry/error classification (see above).
- Posts, carousels, stories, templates and other content beyond text plus one file.
- Provider-specific composer controls. Some help text and pairing flows remain
  WhatsApp-specific. Upload signature/type rules and the series 4096-byte text
  ceiling remain application-wide; an adapter can impose stricter limits.
- Readiness still depends on the built-in WhatsApp worker. Independent optional
  provider health checks need a separate deployment policy.

These boundaries are intentional: contributors can add a token-based direct
messaging adapter now, while larger product and lifecycle features receive a
shared contract instead of being smuggled into transport code.
