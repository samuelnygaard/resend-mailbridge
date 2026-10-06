# AGENTS.md

Context for humans and AI agents working on **resend-mailbridge**. Read this
before changing code. `README.md` is the user-facing doc; this file is the
"what's up and what's down" of the project.

## What this is

A single Docker image (`samuelnygaard/mailbridge`) that bridges **Resend
inbound email** (webhook-based, no IMAP) to **IMAP**, so Libredesk can poll it.
It is the inbound half of Ambolt's self-hosted support desk:

- **Libredesk** — helpdesk UI and ticketing (separate image, same compose stack)
- **Resend** — receives mail for `nelgixa.resend.app`; also sends replies via SMTP
- **mailbridge** — this repo: webhook → Maildir → Dovecot IMAP
- **Coolify** — where it all runs, as one Docker Compose resource

Current mail flow: `support@example.com` (Google Workspace) forwards to
`support@nelgixa.resend.app`. Replies go Libredesk → Resend SMTP directly and
never touch the bridge.

## Status

| Area | State |
|---|---|
| Webhook ingest, raw passthrough, dedupe, reconciler | ✅ done, tested |
| Multi-mailbox routing (`MAILBRIDGE_ROUTES`) | ✅ done, tested |
| Bundled Dovecot + supervisord image | ✅ boots in production (after v2 fixes) |
| Dovecot passwd-file config (multi-mailbox) | ⚠️ written, **not yet verified against a running Dovecot** |
| End-to-end on real Resend → Libredesk | ⚠️ single mailbox reached boot; full loop + sender attribution not yet confirmed |
| CI (`.github/workflows/publish.yml`) | ⚠️ written, never run. Needs `DOCKERHUB_USERNAME` / `DOCKERHUB_TOKEN` repo secrets |
| Reconciler pagination | ❌ not implemented (fetches newest `RECONCILE_LIMIT` only) |
| Monitoring / alerting | ❌ none. `/healthz` exposes the data; nothing consumes it yet |

## Layout

```
app/main.py          FastAPI webhook service + reconciler (all Python logic)
app/requirements.txt fastapi, uvicorn, requests, svix, supervisor
entrypoint.sh        root: validate env, mkdir Maildirs, chown, render Dovecot
                     config + passwd-file, preflight binaries, exec supervisord
supervisord.conf     runs dovecot (root master) and uvicorn (uid 1000)
Dockerfile           python:3.12-slim-bookworm + dovecot-imapd (2.3.x)
docker-compose.yml   full Coolify stack: Libredesk, Postgres, Redis, mailbridge
.env.example         paste into Coolify Developer View
tests/               standalone scripts against a mock Resend API
```

## How it works

1. Resend POSTs `email.received` (metadata only) to `/webhook`.
2. Signature verified with svix (`RESEND_WEBHOOK_SECRET`).
3. Recipient matched against `MAILBRIDGE_ROUTES` → mailbox name, or dropped.
4. `GET /emails/receiving/{id}` → `raw.download_url` → original `.eml` bytes.
5. Atomic Maildir delivery: write `tmp/`, fsync, `rename()` into `new/`.
6. Dovecot serves each mailbox as its own IMAP user on :143.
7. A reconciler thread sweeps `GET /emails/receiving` every 5 min and ingests
   anything missed.

## Invariants — do not break these

- **Never reassemble MIME from JSON on the happy path.** Pass the `raw` bytes
  through untouched. Threading and contact matching in Libredesk depend on the
  original headers. `build_fallback_message()` is a last resort only.
- **The Resend `email_id` is the unique part of the Maildir filename**
  (`<ts>.<email_id>.<host>`). This is the entire dedupe mechanism — retries,
  replays and the reconciler converge on one path. Dedupe must check both
  `new/` and `cur/` (Dovecot renames to `cur/NAME:2,S` once read).
- **Ingest failures return HTTP 500.** That triggers Resend's retries.
  Never swallow the exception.
- **The signed `download_url` must not get an `Authorization` header.**
- **Mailbox names are paths.** Validated in Python (`_sanitise_mailbox`) *and*
  shell (`entrypoint.sh`). Keep both; tests cover `../escape`.
- **First matching route wins** — mail to support@ and sales@ = one ticket.
- **Unrouted mail is dropped by default.** The Resend domain catches every
  address; a catch-all turns spam into tickets.
- **Python side and Dovecot side must agree on paths**: Maildir is
  `$MAILBRIDGE_MAILDIR_ROOT/<mailbox>`, IMAP username = mailbox name.

## Pitfalls we already hit

- **Wrong Dockerfile built** → uvicorn ran as PID 1 as uid 1000, no entrypoint,
  `PermissionError` on `/srv/mail/support`. Tell: `Started server process [1]`.
  There was once a split-services variant; it is gone. Only this Dockerfile exists.
- **Hardcoded binary paths** → `exec: /usr/bin/supervisord: not found`. pip
  installs to `/usr/local/bin`, apt to `/usr/sbin`. Binaries are now resolved
  via `command -v`, preflighted in the entrypoint, and checked at build time.
- **Dovecot 2.4 rewrote the config format.** The generated config is 2.3
  syntax; bookworm ships 2.3.x. Don't bump the base image casually.
- **Libredesk `--config=''` crashes** (koanf `Must*` panics before `--install`).
  The compose omits `--config` and layers `LIBREDESK_*` env vars on the bundled
  config. Libredesk health endpoint is `/health`, not `/api/v1/health`.
- **Libredesk `Scan Inbox Since` is a window, not a cursor** — too short and
  existing Maildir mail is ignored. Use `720h` for first sync.
- **Libredesk TLS dropdown says `OFF`**, not "None", for plaintext IMAP.
- **Webhook secret chicken-and-egg**: it only exists after the endpoint is
  live, so the compose makes it optional (`:-`). Fill it in right after.

## Running locally

```bash
pip install -r app/requirements.txt httpx
python tests/test_bridge.py
python tests/test_routes.py
sh -n entrypoint.sh
```

Tests need no network, Docker or Dovecot. They do **not** exercise Dovecot or
supervisord — those are only proven by a real container. After changing
`entrypoint.sh` or the Dovecot config, verify in a container:

```bash
docker run --rm -e RESEND_API_KEY=x -e MAILBRIDGE_IMAP_PASSWORD=x \
  -e MAILBRIDGE_ROUTES=a@b.c=support samuelnygaard/mailbridge:dev &
docker exec <id> doveconf -n
```

## Working rules for agents

- Ambolt policy applies: branch + PR only, never push to `main`, never merge.
  Use `ambolt-gh` for GitHub. Never commit secrets — `.env` is gitignored.
- Keep `README.md`, `.env.example`, `docker-compose.yml` and this file in sync
  when env vars change.
- Add a test to `tests/` for any change to routing, dedupe or delivery.
- Prefer tagging releases (`v1.2.0`) over relying on `:latest` in production.

## Ideas / next steps

- Validate passwd-file Dovecot config in a real container; add a container
  smoke test to CI.
- Reconciler pagination.
- Optional per-mailbox IMAP passwords.
- Prometheus metrics or an alert when `last_delivery_at` goes stale.
- Pin `libredesk/libredesk` to a version tag in the compose file.
