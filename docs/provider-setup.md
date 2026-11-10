# Telegram, Signal and TikTok setup

The app now discovers seven installed providers: built-in WhatsApp, Whapi.Cloud,
Facebook Page, Instagram, Telegram, Signal and TikTok. Telegram and Signal use the existing message composer,
recipient lists, scheduler, changing text and message series. TikTok uses a separate
publishing page with consent, per-post settings and durable completion tracking.

Run `alembic upgrade head` before starting this version on an existing database.
The `20261110_0014` migration adds only `tiktok_posts`. Existing WhatsApp records
and provider IDs are unchanged. Deployment remains single-instance with SQLite.

## Telegram

1. Create a bot using [@BotFather](https://t.me/BotFather) and obtain its bot token.
2. Open Connections → Add provider → Telegram and paste the token.
3. Use a numeric chat ID (including the minus sign for groups/channels) or a
   public `@channel_username` as the recipient in the normal message composer.
4. Private users must first start the bot. Add the bot to the target group or
   channel and grant it the permissions required to send there.

Text uses `sendMessage`. JPEG/PNG use `sendPhoto`, MP4 uses `sendVideo`, and other
accepted app uploads use `sendDocument`. Original filenames are preserved. The
adapter validates Telegram's 4096-unit text and 1024-unit caption limits, with
UTF-16 counting so emoji are counted correctly. Photos are capped at 10 MB;
other files at 50 MB. The app's existing signature/type restrictions still apply.

Telegram bots do not provide a general endpoint listing all their groups.
The recipient picker returns an empty list; enter chat IDs manually or save a
recipient list. This app does not consume `getUpdates` or replace your bot webhook.
The integration sends as a bot, not as a personal Telegram user account.

Reference: [Telegram Bot API](https://core.telegram.org/bots/api).

## Signal

Signal uses the community-maintained
[signal-cli-rest-api service](https://github.com/bbernhard/signal-cli-rest-api).
It is not a public Signal bot API. Deploy the service separately, persist its
account data, and register or link the sending account using that service's
instructions. The WhatSend Docker image does not bundle the Signal service.

Configure the app host:

```dotenv
SIGNAL_API_URL=http://signal-service:8080
# Optional: bearer token required by your authenticated service proxy.
SIGNAL_API_TOKEN=
```

The origin is controlled by the administrator, never by connection form input.
The underlying bridge may expose linked account keys and messages, so use a
private network or an authenticated proxy. `SIGNAL_API_TOKEN` is only useful if
that proxy actually enforces bearer authentication; the app does not add auth to
the service itself. Set up regular receiving as described in the bridge's docs
so account/group state stays current. No inbound inbox is implemented here.

An operator with the app's environment issues a token for one linked account:

```bash
.venv/bin/python scripts/create_signal_connection.py +201234567890
```

Give that token to the authorized account owner, who selects Signal under
Connections → Add provider. Treat the output as a credential. It contains the
account number and a signature bound to that account, so changing the number
cannot select somebody else's linked account. Simply knowing another sender's
phone number does not grant access. Tokens are encrypted at rest like other
provider credentials. Deactivating/deleting an app connection stops jobs using
that connection. Rotating the app's signing/encryption secret invalidates issued
Signal capabilities; issue new ones afterward. Do not share app-host access with
untrusted users, who could otherwise mint capabilities.

The adapter sends text and base64 attachments through `/v2/send` and discovers
groups through `/v1/groups/{number}`. Use international numbers (`+countrycode…`)
or `group.…` IDs returned by discovery. Message text is capped at 4096 UTF-8 bytes
in this app; attachments at 50 MB plus base64 transport overhead.

Reference: [bridge API documentation](https://bbernhard.github.io/signal-cli-rest-api/).

## TikTok publishing

### Authorization

Register a TikTok developer app, enable Content Posting → Direct Post, obtain
approval for `video.publish`, and authorize the intended creator account. Add a
TikTok connection with that **user access token**, then choose **Publish posts**
on the connection. This is publishing, not direct messaging.

The setup accepts an already-authorized token. It does not implement TikTok Login
Kit, automatic token refresh, or collect client secrets in the browser. Renew
expired credentials through your TikTok app's authorization flow and edit the
connection. Existing publishing records retain a credential fingerprint and do
not silently bind to replacement credentials; review any affected pending or
uncertain posts in TikTok before creating a new submission.

TikTok restricts unaudited clients to private visibility. Public posting requires
TikTok's audit/approval, and its intended-use rules apply to the product itself.
Adding this code does not grant that approval. See the
[Direct Post prerequisites](https://developers.tiktok.com/doc/content-posting-api-get-started/)
and [Content Sharing Guidelines](https://developers.tiktok.com/doc/content-sharing-guidelines/).

### Publish or schedule a post

The publishing page loads the current creator nickname, allowed visibility
options and interaction restrictions from TikTok. Visibility has no default;
comments, Duet and Stitch start off and cannot override disabled account settings.
Users review their content, choose disclosure/AI labels, agree to TikTok's Music
Usage Confirmation, and explicitly authorize the selected account and time.
Branded content cannot be private. Commercial disclosure is validated server-side.

- **Video:** upload one MP4 up to 50 MB. A local preview appears before submission.
  The server reads its movie duration and checks the creator's current duration
  limit. TikTok performs final codec/content validation. The whole file is one
  upload chunk because the app limit is below TikTok's 64 MB single-chunk limit.
  The caption limit is 2200 UTF-16 units.
- **Photos:** supply 1–35 image URLs from a domain/URL prefix verified in your
  TikTok developer portal. TikTok pulls these images; they are not uploaded from
  the app's authenticated media routes. The first image is the cover. The photo
  title limit is 90 UTF-16 units and description limit is 4000. Duet/Stitch controls
  are hidden for photo posts.
- **Time:** leave blank for the next publishing check, or select one future time
  in your account timezone. This creates one durable post, not a recurring
  messaging job. The background check runs every 15 seconds; upload, processing
  and moderation can delay when TikTok makes the content visible.

Enable photo sources with an administrator-owned, verified allowlist:

```dotenv
TIKTOK_PHOTO_URL_PREFIXES=https://media.example.com/tiktok/,https://cdn.example.com/photos/
```

Without that setting only video publishing is shown. Prefix matching checks the
HTTPS origin and path boundary; a similarly named external domain cannot match.
Use valid JPEG/WebP image sources that satisfy TikTok's media requirements.

### Completion, recovery and cancellation

The app creates a `pending` record before any upload and claims it once. It saves
the returned `publish_id` before sending video bytes to TikTok's signed upload
URL. The URL is restricted to HTTPS TikTok API hosts; redirects are not followed,
no access token is sent to the upload URL, and the signed URL is not persisted.

The worker polls the saved publishing ID. Only `PUBLISH_COMPLETE` is shown as
**Published** (`sent` internally). Processing is not success. A failed or
interrupted initialization becomes failed/unknown and is never automatically
initialized again. A lost upload acknowledgement or process restart retains the
existing ID for status checks, without uploading or posting a duplicate.

You can cancel a pending post. Once submission has started, cancellation in this
app cannot retract an external post. Check and manage the content in TikTok.
Deleting a connection cancels its pending posts and marks in-flight work uncertain.
Local video files are removed after upload attempts, cancellation or preflight
failure. Status/history and video previews require the owning user session;
mutations require CSRF protection. The form's submission key prevents double-clicks
and replay from creating another publishing record.

Use `/providers/tiktok` to view your most recent 100 posts and refresh their
statuses. `/providers/tiktok/posts/{id}` returns status JSON for the signed-in
owner. Publishing endpoints use the user session and CSRF protection; the bearer
messaging endpoints and the Claude messaging connector do not publish TikTok posts.

References: [Direct Post](https://developers.tiktok.com/doc/content-posting-api-reference-direct-post/),
[Photo Post](https://developers.tiktok.com/doc/content-posting-api-reference-photo-post/),
[Media Transfer](https://developers.tiktok.com/doc/content-posting-api-media-transfer-guide/),
[Post Status](https://developers.tiktok.com/doc/content-posting-api-reference-get-video-status/).

## Messaging API and delivery failures

Telegram and Signal work with `/api/send` and `/api/groups`. `/api/send/media`
now accepts any linked messaging provider that supports attachments, while the
existing built-in WhatsApp idempotency and operation-status behavior is retained.
API keys remain bound to one connection; a provider name cannot override that
binding. Telegram and Signal media requests are synchronous and do not gain an
idempotency guarantee from an `Idempotency-Key` header.

New adapters return sanitized errors without upstream response bodies, bot-token
URLs, bridge authorization headers or signed upload URLs. An ambiguous send is
recorded as **unknown** and is not retried automatically. Definitive Telegram or
Signal rejections are failed without a blind retry. Rate-limit failures require
a later user-approved attempt. Existing WhatsApp retry policies are unchanged.

Run `.venv/bin/pytest -q` for adapter payload, ownership, consent, credential,
network-failure, scheduling, recovery and migration tests. All provider requests
in these tests use mocked HTTP transports. Live sends, Signal linking and TikTok
app approval require real accounts and were not performed by the test suite.
