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
| Recipient-derived accounts (`MAILBRIDGE_RECIPIENTS`) | Configuration and routing tests cover automatic usernames |
| Bundled Dovecot + supervisord image | ✅ boots in production (after v2 fixes) |
| Dovecot passwd-file config (multi-mailbox) | Container smoke test checks shared-password login and mailbox isolation |
| End-to-end on real Resend → Libredesk | ⚠️ single mailbox reached boot; full loop + sender attribution not yet confirmed |
| CI (`.github/workflows/publish.yml`) | Secrets configured by maintainer; Python/release regressions and native AMD64/ARM64 container tests gate automatic patch releases on main. GitHub execution not yet verified |
| Reconciler pagination | Bounded cursor sweeps and durable failed-ID retries, tested against a mock API |
| Monitoring / alerting | ❌ none. `/healthz` exposes the data; nothing consumes it yet |

`/healthz` authenticates every mailbox and separately reports reconciler health.
Failed or stalled recovery makes it return 503 even when IMAP works. Public
errors contain only safe error types/status codes, never signed URLs.

Recovery uses `RECONCILE_LIMIT` (1–100 items/page) and `RECONCILE_MAX_PAGES`
(2–100 pages/sweep, default 5). Its cursor and bounded failed-ID queue are saved
on the mail volume. `MAILBRIDGE_INGEST_WORKERS` bounds active webhook ingests
(1–32, default 4) inside the single Uvicorn process.

## Layout

```
app/main.py          FastAPI webhook service + reconciler (all Python logic)
app/requirements.txt fastapi, uvicorn, requests, svix, supervisor
.github/scripts/release.cjs GitHub tag reservations and release completion
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
   Without a secret, reject ingestion with 503; keep bootstrap/IMAP running.
3. Recipient matched against `MAILBRIDGE_RECIPIENTS` → local-part mailbox name,
   or legacy `MAILBRIDGE_ROUTES` → explicit mailbox name; unmatched mail dropped.
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
  Serialize ingest/delivery by ID in the single service process; use unique
  temporary files and clean them on failure. Keep Uvicorn at one process unless
  cross-process locking is added. Email IDs must be safe filename components.
- **Ingest failures return HTTP 500.** That triggers Resend's retries.
  Never swallow the exception.
  Offload webhook ingest to bounded threads; return 503 when capacity is full.
  Never acknowledge success before delivery completes or release a worker slot
  merely because the requesting client disconnected.
- **The signed `download_url` must not get an `Authorization` header.**
- **Mailbox names are paths.** Validated in Python (`_sanitise_mailbox`) *and*
  shell (`entrypoint.sh`) before any directory creation. Require canonical names
  matching `[a-z0-9][a-z0-9._-]{0,63}`, preserve whitespace for rejection, and
  disable shell glob expansion. Validate again at Python's path boundary.
  Keep both; tests cover traversal, whitespace and the length boundary.
- **First matching route wins** — mail to support@ and sales@ = one ticket.
- **Recipient-derived accounts use an address allowlist.** `MAILBRIDGE_RECIPIENTS`
  derives usernames from local parts, provisions them at boot, and cannot be
  combined with `MAILBRIDGE_ROUTES`. Different addresses cannot silently share
  a derived username. The ordered allowlist determines routing precedence.
- **All IMAP users share `MAILBRIDGE_IMAP_PASSWORD`.** Store only a salted hash
  in the passwd-file, owned by `root:dovecot` with mode `0640`; the unprivileged
  auth process must be able to read it. Set permissions before atomically
  replacing the file; preserve the previous file on preparation failure.
  Never log passwords or hashes.
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
  live, so the compose makes it optional (`:-`). Without it, /webhook returns
  503; /healthz exposes `webhook_enabled=false`. Fill it in and redeploy.

## Running locally

```bash
pip install -r tests/requirements.txt
python tests/test_bridge.py
python tests/test_routes.py
python tests/test_routes.py --auto
python tests/test_auto_mailboxes.py
python tests/test_reliability.py
python tests/test_reconcile.py
python tests/test_workers.py
node --test tests/test_release.cjs
sh -n entrypoint.sh
```

The scripts above need no external network, Docker or Dovecot. After changing
`entrypoint.sh` or the Dovecot config, also build and run the real container smoke
test. It checks both IMAP users before mail arrives, exact message retrieval,
mailbox isolation, wrong-password/unknown-user rejection and unsafe boot config:

```bash
docker build -t mailbridge:test .
python tests/test_container.py --image mailbridge:test
```

## Working rules for agents

- Ambolt policy applies: branch + PR only, never push to `main`, never merge.
  Use `ambolt-gh` for GitHub. Never commit secrets — `.env` is gitignored.
- Keep `README.md`, `.env.example`, `docker-compose.yml` and this file in sync
  when env vars change.
- Add a test to `tests/` for any change to routing, dedupe or delivery.
- Prefer tagging releases (`v1.2.0`) over relying on `:latest` in production.
- Main publishing reserves the next stable patch version with a Git tag
  (bootstrap baseline `0.1.2`), then uploads and records the digest in a GitHub
  Release. Keep allocation serialized across refs. A reservation survives
  failure; retries reuse it, while completed releases and stale main runs skip.
  Only the publishing job needs `contents: write`. GitHub's built-in token does
  not trigger another workflow for its tag push, so upload stays in this run.

## Ideas / next steps

- Prometheus metrics or an alert when `last_delivery_at` goes stale.
- Pin `libredesk/libredesk` to a version tag in the compose file.
