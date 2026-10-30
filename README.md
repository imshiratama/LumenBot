<div align="center">

# LumenBot

**A better front door for Telegram communities.**

Open-source member verification and access control for Telegram communities, built for serverless deployment.

[English](README.md) · [简体中文](README.zh-CN.md)

[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Telegram Bot API](https://img.shields.io/badge/Telegram-Bot%20API-26A5E4?logo=telegram&logoColor=white)](https://core.telegram.org/bots/api)
[![Vercel](https://img.shields.io/badge/Deployed%20on-Vercel-000000?logo=vercel&logoColor=white)](https://vercel.com/)
[![Upstash](https://img.shields.io/badge/Redis%20%2B%20QStash-Upstash-00C98D?logo=upstash&logoColor=white)](https://upstash.com/)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue)](LICENSE)

</div>

LumenBot welcomes new members with a guided verification flow: profile checks, X follow verification, manual review when needed, and automatic timeout handling. It uses Telegram Webhooks instead of polling, Redis for durable sessions, and QStash for delayed timeout callbacks.

## Features

- **Join protection** — restricts new members immediately and presents a verification card.
- **Profile checks** — verifies a Telegram first name and profile photo.
- **X follow verification** — checks the submitted X account against your target account.
- **Manual actions** — current group administrators can approve or reject from the verification card.
- **Timeout handling** — removes unverified members after a configurable deadline.
- **Concurrent sessions** — uses Redis atomic transitions to coordinate Telegram and QStash callbacks.
- **Protected endpoints** — verifies Telegram secrets, QStash signatures, and administrator credentials.
- **Multiple groups** — supports a configurable group allowlist and per-user verification sessions.

## Architecture

```text
Telegram
  │  chat_member / message / callback_query
  ▼
FastAPI on Vercel
  ├── Upstash Redis: sessions, private-chat bindings, atomic state transitions
  ├── TwitterAPI.io: X follow relationship checks
  └── Upstash QStash: delayed timeout callbacks
```

Built with Python 3.12, `python-telegram-bot` 20.8, FastAPI, HTTPX, Upstash Redis, and QStash. No polling process, in-memory session store, or APScheduler JobQueue is required.

## Before You Start

1. **TwitterAPI.io requires its own API key.** An X developer account is not required, but TwitterAPI.io is not a permanent, keyless, free public API. Its pricing, quota, availability, and data freshness are controlled by the provider.
2. **A submitted X username does not prove account ownership.** LumenBot verifies the submitted account's follow relationship; it is not an identity or X account ownership verification system.
3. **Deployment requires configuration.** Importing the repository is only the first step. You must configure Vercel, Upstash, and the Telegram Webhook.
4. Telegram join-request approval and LumenBot verification are separate flows. If join requests are enabled, LumenBot starts after the member is approved and joins.
5. Deploy only bots and communities you own or are authorized to administer, and review third-party service terms yourself.

## Quick Start

### 1. Prepare services

- Create a bot with [@BotFather](https://t.me/BotFather) and obtain its token.
- Create an Upstash Redis database; record its REST URL and REST token.
- Open Upstash QStash; record its token, Current Signing Key, and Next Signing Key.
- Obtain a TwitterAPI.io API key.
- Prepare your target X username and supergroup ID.

### 2. Import into Vercel

1. Import this repository from [Vercel](https://vercel.com/new).
2. Select **FastAPI** as the Application Preset.
3. Keep Root Directory as `./` and retain the default build settings.
4. Use the stable production domain, such as `https://your-project.vercel.app`. Do not use a deployment-specific URL.

### 3. Configure environment variables

Add these variables under **Project → Settings → Environment Variables** for **Production**. Redeploy after changing them.

#### Required

| Variable | Description |
| --- | --- |
| `TG_TOKEN` | Bot token issued by BotFather. |
| `TARGET_X_ID` | Target X username, without `@`. |
| `TWITTERAPI_KEY` | API key issued by TwitterAPI.io. |
| `ALLOWED_CHAT_IDS` | Supergroup IDs where verification is enabled, usually beginning with `-100`. Separate multiple IDs with commas. Empty enables all groups; an explicit allowlist is recommended. |
| `TELEGRAM_WEBHOOK_SECRET` | Secret used to authenticate Telegram Webhook requests. |
| `ADMIN_SETUP_SECRET` | Secret protecting `/api/setup` and `/api/setup/status`. |
| `PUBLIC_BASE_URL` | Stable production origin, such as `https://your-project.vercel.app`, without a path or trailing `/`. Vercel's `VERCEL_PROJECT_PRODUCTION_URL` can also be used, but an explicit value is recommended. |
| `UPSTASH_REDIS_REST_URL` | Upstash Redis REST URL. |
| `UPSTASH_REDIS_REST_TOKEN` | Upstash Redis REST token. |
| `QSTASH_TOKEN` | QStash token. |
| `QSTASH_CURRENT_SIGNING_KEY` | QStash Current Signing Key. |
| `QSTASH_NEXT_SIGNING_KEY` | QStash Next Signing Key. |

Generate two different webhook/admin secrets locally. Each must contain 16–256 characters using only letters, numbers, `_`, or `-`.

```powershell
[guid]::NewGuid().ToString("N")
```

#### Optional

| Variable | Default | Description |
| --- | --- | --- |
| `VERIFICATION_TIMEOUT_SECONDS` | `300` | Total verification deadline in seconds. Use `600` for ten minutes. |
| `X_REQUEST_TIMEOUT_SECONDS` | `15` | Timeout for an individual X API request. Independent of the overall verification deadline. |
| `MAX_X_ATTEMPTS` | `5` | Maximum X queries per verification session. |
| `X_RETRY_SECONDS` | `10` | Minimum delay between X queries in one session. |
| `X_API_FAIL_OPEN` | `false` | If `true`, X service failures skip the X check and allow access. Default sends the session to manual review. Enable only if you accept the reduced protection. |

Use Vercel **Secret** for credentials and **Config** for ordinary settings. Never commit real credentials to `.env.example`, documentation, or Git.

### 4. Deploy and register the Webhook

Redeploy Production after saving the variables. Telegram and QStash must be able to reach the endpoints without an interactive login or Deployment Protection challenge.

Health check:

```text
https://your-project.vercel.app/api/health
```

A healthy response contains `"ok":true`.

Register the Webhook from local PowerShell. When prompted, enter the **value** of `ADMIN_SETUP_SECRET`:

```powershell
$base = "https://your-project.vercel.app"
$admin = Read-Host "ADMIN_SETUP_SECRET"

Invoke-RestMethod -Uri "$base/api/setup" `
  -Method Post `
  -Headers @{ "X-Admin-Setup-Secret" = $admin }

Invoke-RestMethod -Uri "$base/api/setup/status" `
  -Headers @{ "X-Admin-Setup-Secret" = $admin }
```

The setup response should show `ok: True`. The status response should show `matches: True` with no `last_error_message`.

### 5. Configure the Telegram group

Add the bot as a supergroup administrator with:

- **Ban Users** — required for restricting, unrestricting, removing, and unbanning members.
- **Delete Messages** — required for cleaning up verification and review cards.

Other administrator privileges are unnecessary. Bot Privacy does not control `chat_member` updates, but the bot must be an administrator to receive other members' updates.

Group administrators are detected live through Telegram, so no administrator ID list is required. `ALLOWED_CHAT_IDS` contains group IDs; preserve the leading minus sign and do not use usernames or invite links.

## Verification Flow

1. A `chat_member` transition creates a restricted session, a Redis record, and a delayed QStash callback.
2. The new member receives a group card with an X profile link and verification button.
3. LumenBot checks the member's first name and profile photo count.
4. The button opens a private chat with a signed session-specific `/start` payload. The user sends an X username directly.
5. TwitterAPI.io checks the follow relationship. Only a strict JSON boolean `data.following: true` passes automatically.
6. Successful verification removes the personal restriction and deletes the verification messages.
7. X service failures create an administrator review card by default. The initial verification card also has a separate row of administrator approve/reject buttons. Current group owners and administrators are verified through Telegram, and the deadline continues.
8. Expired sessions trigger a member-status check followed by ban and unban, allowing the removed user to request entry again later. Members already unrestricted by an administrator are not kicked.

Each session belongs to one group and one user. Switching groups requires entering through the new group's current verification card.

## Operations

### No verification card appears

- Confirm the bot is an administrator in the **same group** and can ban users.
- Compare `ALLOWED_CHAT_IDS` with the actual `chat_id` in Runtime Logs, including the `-100` prefix.
- Test with a normal member, not a group owner or administrator.
- Check Runtime Logs for allowlist rejection or restriction failures.

### Automatic X verification is unavailable

Inspect the reason after `X 查询不可用 ...:` in Runtime Logs. Common causes are invalid credentials, exhausted quota, rate limiting, provider outages, or unexpected responses. Failure is never treated as proof of a follow by default.

### `bot can't initiate conversation with a user`

Telegram does not allow a bot to initiate a private conversation with someone who has not started it or has stopped/blocked it. This only affects proactive private notifications, such as timeout messages; it does not cause X verification failures.

### Configuration and domain changes

- Redeploy Production after changing environment variables.
- Changing only group IDs or verification duration does not require Webhook registration again.
- After changing `TELEGRAM_WEBHOOK_SECRET`, run `/api/setup` again.
- `PUBLIC_BASE_URL` determines the callback URL for new QStash tasks. Existing delayed tasks do not migrate automatically after a domain change.

### Security

Telegram Bot API URLs can contain the bot token. Redact them before sharing logs. Revoke or rotate exposed credentials immediately, update Vercel, and redeploy.

### Operational limits

- Timeout removal depends on QStash delivery. Failed scheduling or Telegram ban/unban operations require administrator attention.
- Third-party X data may be delayed, restricted, or unavailable.
- Manual review and manual actions do not extend the verification deadline.
- LumenBot is not a complete anti-spam, community-risk, or identity-verification platform.

## Development

Install runtime dependencies:

```bash
python -m pip install -r requirements.txt
```

Install test tools:

```bash
python -m pip install "pytest>=8,<10" "pytest-asyncio>=0.23,<2" "PyJWT>=2.8,<3"
```

Run tests:

```bash
python -m pytest
```

Start the FastAPI application locally:

```bash
python -m uvicorn guard.web:app --host 127.0.0.1 --port 3000
```

For local development, keep real credentials in an untracked `.env`. Production Telegram and QStash callbacks require a publicly reachable HTTPS endpoint. Tests use an in-memory Redis test double and mocked HTTP; they do not replace end-to-end testing against Redis, Telegram, TwitterAPI.io, or Vercel.

## HTTP Endpoints

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/` | GET | Basic service status page. |
| `/api/health` | GET | Health check without credentials or group configuration. |
| `/api/setup` | POST | Admin-protected Webhook registration and Redis check. |
| `/api/setup/status` | GET | Admin-protected Webhook status. |
| `/api/telegram` | POST | Telegram Update endpoint protected by a Webhook secret. |
| `/api/timeout` | POST | QStash timeout endpoint protected by signature verification. |

## Project Structure

```text
main.py                Vercel ASGI entry point
api/index.py           API entry point
guard/config.py        Environment-based runtime configuration
guard/service.py       Verification, review, removal, and Telegram workflows
guard/store.py         Redis sessions and atomic Lua transitions
guard/web.py           FastAPI routes, Webhooks, and signature verification
tests/                 Automated tests
pyproject.toml         Python and Vercel project configuration
vercel.json            Vercel Function configuration
.env.example           Configuration template without real credentials
```

## Contributing

Issues and pull requests are welcome. For substantial behavior changes, describe the use case and compatibility impact first. Keep secrets out of commits, and test the relevant verification, review, and timeout paths.

## License

Licensed under [Apache License 2.0](LICENSE). Original-author and project attribution is recorded in [NOTICE](NOTICE). Follow the license and NOTICE requirements when distributing derivative works, and indicate your changes.
