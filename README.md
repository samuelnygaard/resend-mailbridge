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

## Quick start (Coolify)

1. New → **Docker Compose Empty**, paste [`docker-compose.yml`](docker-compose.yml).
2. Environment Variables → Developer View → paste [`.env.example`](.env.example), fill in.
3. Assign domains: `app` → `https://support.ambolt.io:9000`, `mailbridge` → `https://hooks.ambolt.io:8080`.
4. Deploy. Check `curl https://hooks.ambolt.io/healthz` → 200, `"imap_ok": true`.
5. Resend → Webhooks → `https://hooks.ambolt.io/webhook`, event `email.received`.
   Put the signing secret in `RESEND_WEBHOOK_SECRET`, redeploy.
6. In Libredesk, add one email inbox **per mailbox** (see below).

## Configuration

| Variable | Required | Default | Notes |
|---|---|---|---|
| `RESEND_API_KEY` | yes | — | Container exits if unset |
| `RESEND_WEBHOOK_SECRET` | strongly | — | Unset ⇒ signatures not verified |
| `MAILBRIDGE_IMAP_PASSWORD` | yes | — | Shared by all mailboxes. In Coolify: `${SERVICE_PASSWORD_MAILBRIDGEIMAP}` |
| `MAILBRIDGE_ROUTES` | yes* | — | `addr=mailbox,addr=mailbox`. First match wins |
| `MAILBRIDGE_DEFAULT_MAILBOX` | no | *(drop)* | Catch-all for unrouted mail. Leave empty |
| `ALLOWED_RECIPIENTS` | no | — | Legacy single-mailbox mode, ignored if routes set |
| `MAILBRIDGE_IMAP_USER` | no | `support` | Legacy single-mailbox name |
| `MAILBRIDGE_MAILDIR_ROOT` | no | `/srv/mail` | Mount a volume here |
| `MAILBRIDGE_HTTP_PORT` / `MAILBRIDGE_IMAP_PORT` | no | `8080` / `143` | |
| `RECONCILE_INTERVAL` / `RECONCILE_LIMIT` | no | `300` / `100` | Recovery sweep |
| `LOG_LEVEL` | no | `INFO` | |

\* required by the compose file; the image itself falls back to single-mailbox mode.

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

```bash
pip install -r app/requirements.txt httpx
python tests/test_bridge.py     # webhook, passthrough, dedupe, reconciler, healthz
python tests/test_routes.py     # multi-mailbox routing
```

Tests run against a mock Resend API — no account or network needed. Building:

```bash
docker buildx build --platform linux/amd64,linux/arm64 -t samuelnygaard/mailbridge:dev .
```

See [AGENTS.md](AGENTS.md) for architecture, invariants and known pitfalls.
