"""
mailbridge - Resend Inbound -> Maildir -> Dovecot IMAP -> Libredesk

Receives Resend `email.received` webhooks, downloads the ORIGINAL RFC822
message, and delivers it into a Maildir served over IMAP by Dovecot running
in the same container.

Passing the original bytes through untouched is the whole point: Message-ID,
In-Reply-To, References, Reply-To, encoded subjects, multipart boundaries,
inline images and attachments all survive, so Libredesk threads conversations
and attributes contacts exactly as it would against a real mailbox.
"""

import email.utils
import copy
import json
import imaplib
import logging
import os
import pathlib
import re
import socket
import threading
import tempfile
import time
from email.message import EmailMessage
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException, Request
from svix.webhooks import Webhook, WebhookVerificationError

# ---------------------------------------------------------------- config

API_BASE = os.environ.get("RESEND_API_BASE", "https://api.resend.com")
API_KEY = os.environ.get("RESEND_API_KEY", "")
WEBHOOK_SECRET = os.environ.get("RESEND_WEBHOOK_SECRET", "")

IMAP_USER = os.environ.get("MAILBRIDGE_IMAP_USER", "support")
MAILDIR_ROOT = pathlib.Path(os.environ.get("MAILBRIDGE_MAILDIR_ROOT", "/srv/mail"))

_SAFE_MAILBOX = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _sanitise_mailbox(name: str) -> str:
    name = name.strip().lower()
    if not _SAFE_MAILBOX.fullmatch(name):
        raise ValueError(
            f"invalid mailbox name {name!r}: use lowercase letters, digits, dot, dash, underscore"
        )
    return name


def parse_routes() -> list:
    """Derive accounts from accepted addresses, or use legacy explicit routes.

    MAILBRIDGE_RECIPIENTS=support@example.com,sales@example.com creates
    support and sales accounts without configuring IMAP usernames.
    MAILBRIDGE_ROUTES maps inbound addresses to mailboxes explicitly:

        support@nelgixa.resend.app=support,sales@nelgixa.resend.app=sales

    Order matters: an email addressed to several of them lands in the first
    match, so it becomes one ticket rather than two.
    """
    recipients = os.environ.get("MAILBRIDGE_RECIPIENTS", "").strip()
    raw = os.environ.get("MAILBRIDGE_ROUTES", "").strip()
    routes = []
    if recipients:
        if raw:
            raise ValueError("set MAILBRIDGE_RECIPIENTS or MAILBRIDGE_ROUTES, not both")
        usernames = {}
        for address in recipients.split(","):
            address = address.strip().lower()
            if not address:
                continue
            local, separator, domain = address.partition("@")
            if not separator or len(domain) > 253 or not re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*",
                domain,
            ):
                raise ValueError(f"invalid address in MAILBRIDGE_RECIPIENTS: {address!r}")
            mailbox = _sanitise_mailbox(local)
            if mailbox != local:
                raise ValueError(f"invalid address in MAILBRIDGE_RECIPIENTS: {address!r}")
            if mailbox in usernames:
                if usernames[mailbox] == address:
                    continue
                raise ValueError(
                    f"addresses {usernames[mailbox]!r} and {address!r} derive the same "
                    f"IMAP username {mailbox!r}; use MAILBRIDGE_ROUTES for explicit mappings"
                )
            usernames[mailbox] = address
            routes.append((address, mailbox))
        if not routes:
            raise ValueError("MAILBRIDGE_RECIPIENTS must contain at least one address")
        return routes
    if raw:
        for pair in raw.split(","):
            pair = pair.strip()
            if not pair:
                continue
            if "=" not in pair:
                raise ValueError(
                    f"bad MAILBRIDGE_ROUTES entry {pair!r}: expected address=mailbox"
                )
            addr, mbox = pair.split("=", 1)
            routes.append((addr.strip().lower(), _sanitise_mailbox(mbox)))
        return routes

    # Back-compat: no routes defined, so everything allowed goes to one mailbox.
    allowed = os.environ.get("ALLOWED_RECIPIENTS", "").strip()
    box = _sanitise_mailbox(IMAP_USER)
    return [(a.strip().lower(), box) for a in allowed.split(",") if a.strip()]


ROUTES = parse_routes()
ROUTE_MAP = dict(ROUTES)
MAILBOXES = sorted({m for _, m in ROUTES}) or [_sanitise_mailbox(IMAP_USER)]

# Where unmatched mail goes. Empty means drop it, which is what you want when
# the receiving domain catches every address.
DEFAULT_MAILBOX = os.environ.get("MAILBRIDGE_DEFAULT_MAILBOX", "").strip().lower()
if DEFAULT_MAILBOX:
    DEFAULT_MAILBOX = _sanitise_mailbox(DEFAULT_MAILBOX)
    if DEFAULT_MAILBOX not in MAILBOXES:
        MAILBOXES.append(DEFAULT_MAILBOX)
elif not ROUTES:
    DEFAULT_MAILBOX = _sanitise_mailbox(IMAP_USER)


def maildir_for(mailbox: str) -> pathlib.Path:
    return MAILDIR_ROOT / mailbox

IMAP_PORT = int(os.environ.get("MAILBRIDGE_IMAP_PORT", "143"))
IMAP_PASSWORD = os.environ.get("MAILBRIDGE_IMAP_PASSWORD", "")
RECONCILE_INTERVAL = int(os.environ.get("RECONCILE_INTERVAL", "300"))
RECONCILE_LIMIT = int(os.environ.get("RECONCILE_LIMIT", "100"))
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "30"))
DOWNLOAD_TIMEOUT = int(os.environ.get("DOWNLOAD_TIMEOUT", "180"))
RECONCILER_ENABLED = os.environ.get("DISABLE_RECONCILER") != "1"
STARTED_AT = time.time()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("mailbridge")

HEADERS = {"Authorization": f"Bearer {API_KEY}"}
HOSTNAME = socket.gethostname().replace("/", "_").replace(":", "_")

app = FastAPI(title="mailbridge")
STATS_LOCK = threading.Lock()
# Bounded lock storage. The same email ID always uses the same reentrant lock,
# including nested ingest -> deliver calls and different mailbox destinations.
EMAIL_LOCKS = tuple(threading.RLock() for _ in range(64))


def email_lock(email_id: str):
    if not isinstance(email_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", email_id):
        raise ValueError("invalid email_id")
    return EMAIL_LOCKS[hash(email_id) % len(EMAIL_LOCKS)]

STATS = {
    "delivered": 0,
    "skipped_duplicate": 0,
    "skipped_recipient": 0,
    "errors": 0,
    "last_delivery_at": None,
    "last_error": None,
    "last_reconcile_at": None,
    "last_reconcile_attempt_at": None,
    "last_reconcile_error": None,
    "reconcile_failures": 0,
    "per_mailbox": {},
}


def safe_error(exc: Exception) -> str:
    """Public diagnostics must never contain exception URLs or credentials."""
    if isinstance(exc, UpstreamError):
        return str(exc)
    return type(exc).__name__


def record_error(exc: Exception) -> None:
    with STATS_LOCK:
        STATS["errors"] += 1
        STATS["last_error"] = safe_error(exc)


class UpstreamError(RuntimeError):
    def __init__(self, status: Optional[int]):
        super().__init__(f"upstream HTTP {status}" if status else "upstream transport failure")

# ---------------------------------------------------------------- maildir


def ensure_maildir(mailbox: Optional[str] = None) -> None:
    boxes = [mailbox] if mailbox else MAILBOXES
    for box in boxes:
        for sub in ("tmp", "new", "cur"):
            (maildir_for(box) / sub).mkdir(parents=True, exist_ok=True)


def already_delivered(email_id: str, mailbox: str) -> bool:
    """Dovecot renames new/NAME to cur/NAME:2,S once a client reads it,
    so both directories must be checked."""
    email_lock(email_id)  # Validate IDs before using them as a glob or path.
    root = maildir_for(mailbox)
    for sub in ("new", "cur"):
        if any((root / sub).glob(f"*.{email_id}.*")):
            return True
    return False


def delivered_anywhere(email_id: str) -> bool:
    return any(already_delivered(email_id, box) for box in MAILBOXES)


def deliver(raw: bytes, email_id: str, mailbox: str) -> bool:
    with email_lock(email_id):
        return _deliver(raw, email_id, mailbox)


def _deliver(raw: bytes, email_id: str, mailbox: str) -> bool:
    """Atomic Maildir delivery.

    The Resend email id becomes the unique part of the filename, so
    redelivery is idempotent without tracking any state of our own: webhook
    retries, dashboard replays and the reconciler all converge on one path.
    """
    ensure_maildir(mailbox)
    if delivered_anywhere(email_id):
        with STATS_LOCK:
            STATS["skipped_duplicate"] += 1
        log.debug("already delivered, skipping %s", email_id)
        return False

    root = maildir_for(mailbox)
    name = f"{int(time.time())}.{email_id}.{HOSTNAME}"
    new_path = root / "new" / name

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=root / "tmp", prefix=name + ".", delete=False) as fh:
            tmp_path = pathlib.Path(fh.name)
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.rename(tmp_path, new_path)  # atomic within one filesystem
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

    with STATS_LOCK:
        STATS["delivered"] += 1
        STATS["per_mailbox"][mailbox] = STATS["per_mailbox"].get(mailbox, 0) + 1
        STATS["last_delivery_at"] = time.time()
    log.info("delivered %s -> %s (%d bytes)", email_id, mailbox, len(raw))
    return True


# ---------------------------------------------------------------- resend


def _get(url: str, **kw) -> requests.Response:
    timeout = kw.pop("timeout", HTTP_TIMEOUT)
    status = None
    for attempt in range(3):
        try:
            resp = requests.get(url, timeout=timeout, **kw)
            status = resp.status_code
            resp.raise_for_status()
            return resp
        except requests.RequestException:
            if attempt < 2:
                time.sleep(2 ** attempt)
    raise UpstreamError(status) from None


def fetch_metadata(email_id: str) -> dict:
    return _get(f"{API_BASE}/emails/receiving/{email_id}", headers=HEADERS).json()


def build_fallback_message(meta: dict) -> bytes:
    """Reconstruct a message when `raw` is unavailable.

    Lossy (attachments are not inlined), so it is a last resort only - but it
    means a ticket is never silently lost.
    """
    msg = EmailMessage()
    for header, value in (meta.get("headers") or {}).items():
        if header.lower() in {"content-type", "content-transfer-encoding", "mime-version"}:
            continue
        try:
            msg[header] = value
        except Exception:  # noqa: BLE001
            continue

    if "From" not in msg:
        msg["From"] = meta.get("from", "unknown@invalid")
    if "To" not in msg:
        msg["To"] = ", ".join(meta.get("to") or [])
    if "Subject" not in msg:
        msg["Subject"] = meta.get("subject") or "(no subject)"
    if "Message-ID" not in msg and meta.get("message_id"):
        msg["Message-ID"] = meta["message_id"]
    if "Date" not in msg:
        msg["Date"] = email.utils.formatdate(localtime=True)

    msg.set_content(meta.get("text") or "(no plain text body)")
    if meta.get("html"):
        msg.add_alternative(meta["html"], subtype="html")

    names = [a.get("filename") for a in (meta.get("attachments") or []) if a.get("filename")]
    if names:
        msg.add_attachment(
            ("Attachments not retrieved by the bridge:\n" + "\n".join(names)).encode(),
            maintype="text", subtype="plain", filename="ATTACHMENTS-MISSING.txt",
        )

    log.warning("no raw content for %s, using reconstructed fallback", meta.get("id"))
    return msg.as_bytes()


def fetch_message(email_id: str) -> bytes:
    meta = fetch_metadata(email_id)
    download_url = (meta.get("raw") or {}).get("download_url")
    if download_url:
        # Signed URL - must NOT carry the Authorization header.
        return _get(download_url, timeout=DOWNLOAD_TIMEOUT).content
    return build_fallback_message(meta)


def recipients_of(payload: dict) -> list:
    out = []
    for key in ("to", "received_for", "cc", "bcc"):
        for addr in payload.get(key) or []:
            _, parsed = email.utils.parseaddr(addr)
            out.append((parsed or addr).lower())
    return out


def mailbox_for(payload: dict) -> Optional[str]:
    """First matching route wins, so a message addressed to both support@ and
    sales@ becomes one ticket instead of two."""
    targets = set(recipients_of(payload))
    for addr, mbox in ROUTES:
        if addr in targets:
            return mbox
    return DEFAULT_MAILBOX or None


def ingest(email_id: str, hint: Optional[dict] = None) -> None:
    with email_lock(email_id):
        _ingest(email_id, hint)


def _ingest(email_id: str, hint: Optional[dict] = None) -> None:
    if delivered_anywhere(email_id):
        return

    payload = hint
    if payload is None or not recipients_of(payload):
        # Webhook payloads carry `to`; if a caller passes none, ask the API
        # rather than guessing the destination.
        payload = fetch_metadata(email_id)

    mailbox = mailbox_for(payload)
    if not mailbox:
        with STATS_LOCK:
            STATS["skipped_recipient"] += 1
        log.info(
            "no route for %s (recipients=%s), skipping",
            email_id, recipients_of(payload) or "none",
        )
        return

    deliver(fetch_message(email_id), email_id, mailbox)


# ---------------------------------------------------------------- routes


@app.post("/webhook")
async def webhook(request: Request):
    if not WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="webhook signing is not configured")
    body = await request.body()

    try:
        Webhook(WEBHOOK_SECRET).verify(body, dict(request.headers))
    except WebhookVerificationError:
        log.warning("rejected webhook with bad signature")
        raise HTTPException(status_code=401, detail="invalid signature")

    # Svix 2.x verifies signatures without returning the parsed JSON event.
    try:
        event = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="invalid JSON") from None
    if not isinstance(event, dict):
        raise HTTPException(status_code=400, detail="event must be an object")

    if event.get("type") != "email.received":
        return {"ok": True, "ignored": event.get("type")}

    data = event.get("data")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="data must be an object")
    email_id = data.get("email_id") or data.get("id")
    if not email_id:
        raise HTTPException(status_code=400, detail="missing email_id")
    try:
        email_lock(email_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid email_id") from None

    try:
        ingest(email_id, hint=data)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        record_error(exc)
        log.error("ingest failed for %s: %s", email_id, safe_error(exc))
        # 500 makes Resend retry on its own schedule; the reconciler is the
        # second safety net. Never swallow this.
        raise HTTPException(status_code=500, detail="ingest failed") from exc

    return {"ok": True, "email_id": email_id}


def imap_reachable() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", IMAP_PORT), timeout=3) as sock:
            return sock.recv(4).startswith(b"* OK")
    except Exception:  # noqa: BLE001
        return False


def imap_authenticated() -> bool:
    if not IMAP_PASSWORD:
        return False
    deadline = time.monotonic() + 3
    try:
        for box in MAILBOXES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            with imaplib.IMAP4("127.0.0.1", IMAP_PORT, timeout=remaining) as client:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                client.sock.settimeout(remaining)
                if client.login(box, IMAP_PASSWORD)[0] != "OK":
                    return False
        return True
    except (OSError, imaplib.IMAP4.error):
        return False


def reconciler_healthy() -> bool:
    if not RECONCILER_ENABLED:
        return True
    last_attempt = STATS["last_reconcile_attempt_at"] or STARTED_AT
    return (STATS["last_reconcile_error"] is None
            and time.time() - last_attempt < max(60, RECONCILE_INTERVAL * 2))


@app.get("/healthz")
def healthz():
    ensure_maildir()
    imap_ok = imap_reachable() and imap_authenticated()
    reconcile_ok = reconciler_healthy()
    unread = {box: len(list((maildir_for(box) / "new").iterdir())) for box in MAILBOXES}
    with STATS_LOCK:
        stats = copy.deepcopy(STATS)
    body = {
        "ok": imap_ok and reconcile_ok,
        "imap_ok": imap_ok,
        "reconcile_enabled": RECONCILER_ENABLED,
        "reconcile_ok": reconcile_ok,
        "webhook_enabled": bool(WEBHOOK_SECRET),
        "maildir_root": str(MAILDIR_ROOT),
        "mailboxes": MAILBOXES,
        "routes": {a: m for a, m in ROUTES},
        "default_mailbox": DEFAULT_MAILBOX or None,
        "unread_in_new": unread,
        "unread_total": sum(unread.values()),
        **stats,
    }
    if not body["ok"]:
        raise HTTPException(status_code=503, detail=body)
    return body


# ---------------------------------------------------------------- reconciler


def _reconcile_sweep() -> bool:
    payload = _get(
        f"{API_BASE}/emails/receiving", headers=HEADERS,
        params={"limit": RECONCILE_LIMIT},
    ).json()
    succeeded = True
    for item in payload.get("data") or payload.get("emails") or []:
        email_id = item.get("id") or item.get("email_id")
        if not email_id:
            continue
        try:
            ingest(email_id, hint=item)
        except Exception as exc:  # noqa: BLE001
            succeeded = False
            record_error(exc)
            STATS["last_reconcile_error"] = safe_error(exc)
            log.error("reconcile failed for %s: %s", email_id, safe_error(exc))
    return succeeded


def reconcile_once() -> None:
    STATS["last_reconcile_attempt_at"] = time.time()
    try:
        succeeded = _reconcile_sweep()
    except Exception as exc:
        record_error(exc)
        STATS["last_reconcile_error"] = safe_error(exc)
        STATS["reconcile_failures"] += 1
        raise
    if succeeded:
        STATS["last_reconcile_error"] = None
        STATS["last_reconcile_at"] = time.time()
        STATS["reconcile_failures"] = 0
    else:
        STATS["reconcile_failures"] += 1


def reconcile_loop() -> None:
    """Safety net. Resend stores every inbound email whether or not our
    endpoint answered, so sweeping the list endpoint recovers anything the
    webhook path missed. This is what makes the design safe, not just clever."""
    time.sleep(10)
    while True:
        try:
            reconcile_once()
        except Exception as exc:  # noqa: BLE001
            log.error("reconcile sweep failed: %s", safe_error(exc))
        time.sleep(RECONCILE_INTERVAL)


@app.on_event("startup")
def _startup() -> None:
    ensure_maildir()
    for addr, mbox in ROUTES:
        log.info("route: %s -> mailbox %s", addr, mbox)
    if DEFAULT_MAILBOX:
        log.info("unmatched mail -> mailbox %s", DEFAULT_MAILBOX)
    else:
        log.info("unmatched mail is dropped (no MAILBRIDGE_DEFAULT_MAILBOX set)")
    log.info(
        "mailbridge up - root=%s mailboxes=%s reconcile=%ss",
        MAILDIR_ROOT, ",".join(MAILBOXES), RECONCILE_INTERVAL,
    )
    if RECONCILER_ENABLED:
        threading.Thread(target=reconcile_loop, daemon=True, name="reconciler").start()
