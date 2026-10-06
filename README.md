# resend-mailbridge

Turns **Resend inbound email** into **IMAP mailboxes**, so a helpdesk that only
speaks IMAP — [Libredesk](https://github.com/abhinavxd/libredesk) in our case —
can poll it. One Docker image: a small webhook service plus Dovecot.

Image: [`samuelnygaard/mailbridge`](https://hub.docker.com/r/samuelnygaard/mailbridge)

```
support@example.com --(Workspace forward)--> support@nelgixa.resend.app
  -> Resend receives + stores
  -> email.received webhook          -> mailbridge :8080 /webhook
  -> GET /emails/receiving/{id}      -> raw.download_url -> original .eml
  -> routed by recipient             -> Maildir /srv/mail/<mailbox>/
  -> Dovecot :143 (internal only)    -> Libredesk polls, one inbox per mailbox
  -> replies leave via Resend SMTP (not through the bridge)
```

The bridge passes Resend's **original message bytes** through untouched, so
`Message-ID`, `In-Reply-To`, attachments and encoded subjects survive and
Libredesk threads replies correctly.

Webhook and recovery deliveries of the same email are serialized across
mailboxes in the single Uvicorn process. Unique temporary files are removed on
failure; retries check both `new/` and Dovecot's renamed files in `cur/`.

## Quick start (Coolify)

1. New → **Docker Compose Empty**, paste [`docker-compose.yml`](docker-compose.yml).
2. Environment Variables → Developer View → paste [`.env.example`](.env.example), fill in.
3. Assign domains: `app` → `https://support.ambolt.io:9000`, `mailbridge` → `https://hooks.ambolt.io:8080`.
4. Deploy. Check `curl https://hooks.ambolt.io/healthz` → 200, `"imap_ok": true`.
5. Resend → Webhooks → `https://hooks.ambolt.io/webhook`, event `email.received`.
   Put the signing secret in `RESEND_WEBHOOK_SECRET`, redeploy. Until then,
   `/webhook` returns 503 and accepts no messages; IMAP and API recovery work.
6. In Libredesk, add one email inbox **per mailbox** (see below).

Set accepted recipient addresses without configuring IMAP usernames:

```dotenv
MAILBRIDGE_RECIPIENTS=support@example.resend.app,sales@example.resend.app
```

The bridge automatically provisions `support` and `sales` accounts at startup,
before any mail arrives. Both use `MAILBRIDGE_IMAP_PASSWORD`; each sees only its
own IMAP `INBOX`. Unlisted addresses, including other addresses on the same
catch-all domain, are dropped. The first configured address matching a message
wins, so mail addressed to both accounts produces one ticket.

Names are derived from the lowercase local part: 1–64 characters, starting with
a letter or digit, followed by letters, digits, dots, dashes or underscores.
Unsupported local parts such as `support+tag` fail startup rather than being
silently renamed. Two different addresses with the same local part also fail
startup; use explicit `MAILBRIDGE_ROUTES` for intentional shared/custom mappings.
Both startup validators check every canonical name before creating directories;
Python also validates names whenever constructing a Maildir path.

## Configuration

| Variable | Required | Default | Notes |
|---|---|---|---|
| `RESEND_API_KEY` | yes | — | Container exits if unset |
| `RESEND_WEBHOOK_SECRET` | yes for webhooks | — | Unset ⇒ webhook requests rejected with 503; bootstrap stays running |
| `MAILBRIDGE_IMAP_PASSWORD` | yes | — | Shared by all mailboxes. In Coolify: `${SERVICE_PASSWORD_MAILBRIDGEIMAP}` |
| `MAILBRIDGE_RECIPIENTS` | yes in supplied Compose | — | Accepted addresses, comma-separated; usernames derive from local parts |
| `MAILBRIDGE_ROUTES` | no | — | Legacy/custom `addr=mailbox` mappings; cannot combine with `MAILBRIDGE_RECIPIENTS` |
| `MAILBRIDGE_DEFAULT_MAILBOX` | no | *(drop)* | Catch-all for unrouted mail. Leave empty |
| `ALLOWED_RECIPIENTS` | no | — | Legacy single-mailbox mode, ignored if routes set |
| `MAILBRIDGE_IMAP_USER` | no | `support` | Legacy single-mailbox name |
| `MAILBRIDGE_MAILDIR_ROOT` | no | `/srv/mail` | Mount a volume here |
| `MAILBRIDGE_HTTP_PORT` / `MAILBRIDGE_IMAP_PORT` | no | `8080` / `143` | |
| `RECONCILE_INTERVAL` / `RECONCILE_LIMIT` | no | `300` / `100` | Recovery sweep |
| `RECONCILE_MAX_PAGES` | no | `5` | List pages per sweep, 2–100; page size 1–100 |
| `MAILBRIDGE_INGEST_WORKERS` | no | `4` | Active webhook ingests, 1–32; excess requests receive 503 for retry |
| `LOG_LEVEL` | no | `INFO` | |

When migrating from explicit routes, replace `MAILBRIDGE_ROUTES` with
`MAILBRIDGE_RECIPIENTS` and redeploy. Existing `support` and `sales` Maildirs and
the shared password are preserved. Renaming a custom mailbox changes its path;
retain explicit routes if you need the old name. The image retains legacy
single-mailbox behavior when neither setting is supplied, so configure the
recipient allowlist explicitly for production.

The supplied Compose stack requires `MAILBRIDGE_RECIPIENTS`. For custom mailbox
names, replace that environment entry with `MAILBRIDGE_ROUTES` in your own
Compose definition; do not pass both to the container.

The shared password is stored as a salted hash in Dovecot's passwd-file. Only
root and Dovecot's authentication group can read it; unknown usernames and
incorrect passwords are rejected. The file is replaced atomically only after
its owner and permissions are set, so a failed preparation preserves accounts.

## Libredesk inbox settings

Admin → Inboxes → New → Email → manual IMAP/SMTP. One inbox per mailbox; only
**Username** and **From** differ.

| IMAP | | SMTP | |
|---|---|---|---|
| Host | `mailbridge` | Host | `smtp.resend.com` |
| Port | `143` | Port | `587` |
| TLS | **OFF** | TLS | STARTTLS |
| Mailbox | `INBOX` | Auth protocol | Plain |
| Username | mailbox name, e.g. `support` | Username | `resend` |
| Password | `SERVICE_PASSWORD_MAILBRIDGEIMAP` | Password | Resend API key |
| Scan Inbox Since | `720h` | Max conns / retries | `5` / `3` |
| Scan Interval | `30s` | Idle / wait timeout | `30s` / `40s` |

From address: `Support <support@example.com>`. Verify `ambolt.io` for **sending**
in Resend — receiving and sending are separate domain configs.

## Development

`/healthz` returns 503 if any configured account cannot authenticate to IMAP,
or if the enabled reconciler fails or stops making progress. `imap_ok` and
`reconcile_ok` identify the failing component. `last_reconcile_attempt_at` tracks
attempts; `last_reconcile_at` records successful sweeps. Diagnostics expose error
types and HTTP status codes, never signed download URLs or exception contents.
Each health check logs into every mailbox and logs out cleanly. Dovecot's
periodic local login/logout messages are expected; health checks do not open
an additional connection that closes partway through the IMAP greeting.
The Compose `mailbridge.healthcheck` calls this endpoint every 30 seconds, with
a 25-second startup grace period and five retries before marking it unhealthy.
It uses the image's Python runtime and overrides the built-in image health
check with the same timings. Docker health status alone does not restart the
container; `restart: unless-stopped` applies when the process exits.

```bash
pip install -r tests/requirements.txt
python tests/test_bridge.py     # webhook, passthrough, dedupe, reconciler, healthz
python tests/test_routes.py     # multi-mailbox routing
python tests/test_routes.py --auto  # routing with recipient-derived usernames
python tests/test_auto_mailboxes.py # configuration boundaries and compatibility
python tests/test_reliability.py   # security and recovery regressions
python tests/test_reconcile.py     # bounded pagination and durable retries
python tests/test_workers.py       # event-loop responsiveness and capacity
node --test tests/test_release.cjs # automatic release allocation and retries
sh -n entrypoint.sh
```

Python tests run against a mock Resend API — no account or network needed.
The release tests require Node.js 24 and use GitHub API fixtures without making
external requests.

Recovery follows Resend's `has_more`/`after` pagination. Each sweep checks the
newest page and resumes older history, up to `RECONCILE_MAX_PAGES` pages. The
cursor and failed IDs live in `.reconcile-state.json` on the Maildir volume and
survive restarts. Up to `RECONCILE_LIMIT` failed IDs are retried each sweep;
successful messages still deduplicate solely against their Maildir filenames.
The retry list holds up to 1,000 IDs; at capacity a failing page keeps its cursor
for the next sweep. Health exposes pending count and safe recovery errors.

Webhook downloads, retries and filesystem writes run in bounded worker threads.
Health requests remain responsive during slow ingestion. A webhook returns 200
only after ingestion completes; failures return 500 and capacity exhaustion
returns 503 with `Retry-After`. Client disconnects do not release capacity until
the underlying work finishes. Recovery uses its own single background thread.

Build the image:

```bash
docker buildx build --platform linux/amd64,linux/arm64 -t samuelnygaard/mailbridge:dev .
```

See [AGENTS.md](AGENTS.md) for architecture, invariants and known pitfalls.

For a local container smoke test (real Dovecot authentication and mailbox
isolation, dummy credentials, no published ports or Resend calls):

```bash
docker build -t mailbridge:test .
python tests/test_container.py --image mailbridge:test
docker run --rm --entrypoint python -v "$PWD:/workspace:ro" -w /workspace mailbridge:test tests/test_entrypoint.py
```

## Publishing to Docker Hub

The GitHub Actions workflow publishes `samuelnygaard/mailbridge` after Python
regressions and real Dovecot container smoke tests pass on native AMD64 and
ARM64 runners. Pull requests and manual runs on other branches run checks only.

Configure repository secrets `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN`; the
token must have write access to `samuelnygaard/mailbridge`. Publishing uses:

- Successful pushes to `main`: automatically increment the patch version and
  publish that version, `latest`, and `sha-<full-commit-SHA>`. The bootstrap
  baseline is `0.1.2`, so the first new release is `0.1.3` unless a higher stable
  Git tag already exists. Versions are compared numerically; prereleases are
  excluded from automatic version allocation.
- Version tags such as `v1.2.3`: `1.2.3` and the commit tag. Prereleases such
  as `v1.2.3-rc.1` use their full version. Release tags leave `latest` unchanged;
  shared minor-version aliases are omitted to avoid races between releases.
- **Actions → Test and publish image → Run workflow**: choose `main` to finish
  an incomplete release for the current commit. A completed release is skipped;
  an untagged current commit receives the next patch version.

The publishing job has `contents: write` permission to create version tags and
GitHub Releases using GitHub's built-in token; no extra secret is needed. All
publishing jobs share a concurrency group with up to 100 pending jobs. After
tests pass, a Git tag reserves the version for the commit. Upload failures leave
that reservation for retries, so failed or abandoned releases can leave gaps
in published versions. After
upload succeeds, the workflow creates a GitHub Release with generated notes
and records the image digest. Retries of completed releases skip publishing;
older `main` commits are also skipped to avoid moving `latest` backwards.
If an explicit tag already published the current main commit, its recorded
image digest is promoted to `latest` without rebuilding or incrementing again.

Explicit release tags must use `vMAJOR.MINOR.PATCH` or a semantic prerelease
suffix; build metadata (`+...`) is unsupported. Malformed tags fail before any
image is published. Image upload happens in the same workflow: tags created by
GitHub's built-in token do not trigger another push workflow. Changing files
locally does not publish; push the changes before starting a run in GitHub.
Maintainer-reported secrets are configured; automatic release publishing has
not yet been verified by a GitHub Actions run.
