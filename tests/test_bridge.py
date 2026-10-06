import tempfile, pathlib as _pl
APP_DIR = str(_pl.Path(__file__).resolve().parent.parent / "app")
TMP = tempfile.mkdtemp(prefix="mailbridge-test-")
import base64, json, os, threading, time, secrets
from http.server import BaseHTTPRequestHandler, HTTPServer

RAW_EML = (
    b"Message-ID: <CAF=orig123@mail.gmail.com>\r\n"
    b"In-Reply-To: <prev999@mail.gmail.com>\r\n"
    b"References: <prev999@mail.gmail.com>\r\n"
    b"From: Jane Customer <jane@customer-example.com>\r\n"
    b"To: support@example.com\r\n"
    b"Subject: =?UTF-8?Q?Faktura=20sp=C3=B8rgsm=C3=A5l?=\r\n"
    b"MIME-Version: 1.0\r\n"
    b"Content-Type: multipart/mixed; boundary=\"BOUND\"\r\n"
    b"\r\n--BOUND\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nHej, jeg har et problem.\r\n"
    b"\r\n--BOUND\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; filename=\"invoice.pdf\"\r\n\r\n%PDF-1.4 fake\r\n"
    b"\r\n--BOUND--\r\n"
)
EMAIL_ID = "56761188-7520-42d8-8898-ff6fc54ce618"
EMAIL_ID2 = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
HITS = {"meta": 0, "raw": 0, "list": 0}

class Mock(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _j(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        p = self.path.split("?")[0]
        if p == f"/emails/receiving/{EMAIL_ID}":
            HITS["meta"] += 1
            assert self.headers.get("Authorization") == "Bearer re_test_key", "missing auth"
            return self._j({"id": EMAIL_ID, "from":"jane@customer-example.com",
                "to":["support@nelgixa.resend.app"], "subject":"Faktura",
                "raw":{"download_url": f"http://127.0.0.1:{PORT}/raw/{EMAIL_ID}",
                       "expires_at":"2026-08-01T00:00:00Z"}})
        if p == f"/emails/receiving/{EMAIL_ID2}":
            # no raw -> exercises the fallback reconstruction path
            return self._j({"id": EMAIL_ID2, "from":"bob@x.com",
                "to":["support@nelgixa.resend.app"], "subject":"No raw here",
                "text":"plain body","html":"<b>html body</b>",
                "message_id":"<noraw@x.com>", "headers":{"X-Custom":"yes"},
                "attachments":[{"filename":"a.png"}], "raw": None})
        if p.startswith("/raw/"):
            HITS["raw"] += 1
            assert "Authorization" not in self.headers, "signed URL must not carry auth header"
            self.send_response(200); self.send_header("Content-Type","message/rfc822")
            self.send_header("Content-Length",str(len(RAW_EML))); self.end_headers()
            return self.wfile.write(RAW_EML)
        if p == "/emails/receiving":
            HITS["list"] += 1
            return self._j({"data":[{"id":EMAIL_ID,"to":["support@nelgixa.resend.app"]},
                                    {"id":EMAIL_ID2,"to":["support@nelgixa.resend.app"]}]})
        self._j({"error":"nf"}, 404)

srv = HTTPServer(("127.0.0.1", 0), Mock); PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

SECRET = "whsec_" + base64.b64encode(secrets.token_bytes(24)).decode()
os.environ.update(
    RESEND_API_BASE=f"http://127.0.0.1:{PORT}", RESEND_API_KEY="re_test_key",
    RESEND_WEBHOOK_SECRET=SECRET, MAILBRIDGE_MAILDIR_ROOT=TMP,
    DISABLE_RECONCILER="1", ALLOWED_RECIPIENTS="support@nelgixa.resend.app",
    MAILBRIDGE_IMAP_PORT="14300", MAILBRIDGE_IMAP_USER="support",
    MAILBRIDGE_IMAP_PASSWORD="bridge-test-password",
)
os.environ.pop("MAILDIR", None)
os.environ.pop("MAILBRIDGE_RECIPIENTS", None)
os.environ.pop("MAILBRIDGE_ROUTES", None)
os.environ.pop("MAILBRIDGE_DEFAULT_MAILBOX", None)
import sys; sys.path.insert(0, APP_DIR)
import main
from fastapi.testclient import TestClient
from svix.webhooks import Webhook

MAILDIR = str(_pl.Path(TMP) / "support")
client = TestClient(main.app)

def post(payload):
    body = json.dumps(payload)
    mid, ts = "msg_test", int(time.time())
    sig = Webhook(SECRET).sign(mid, __import__("datetime").datetime.fromtimestamp(ts, __import__("datetime").timezone.utc), body)
    return client.post("/webhook", content=body, headers={
        "svix-id": mid, "svix-timestamp": str(ts), "svix-signature": sig,
        "content-type": "application/json"})

ev = {"type":"email.received","data":{"email_id":EMAIL_ID,"to":["support@nelgixa.resend.app"]}}

print("== 1. valid signed webhook ==")
r = post(ev); print("  status", r.status_code, r.json()); assert r.status_code == 200

print("== 2. bytes on disk are byte-identical to the original .eml ==")
import pathlib
new = list(pathlib.Path(MAILDIR).joinpath("new").iterdir())
assert len(new) == 1, new
disk = new[0].read_bytes()
print("  identical:", disk == RAW_EML, f"({len(disk)} bytes)")
assert disk == RAW_EML
import email as em
m = em.message_from_bytes(disk)
print("  Message-ID :", m["Message-ID"])
print("  In-Reply-To:", m["In-Reply-To"])
print("  From       :", m["From"])
print("  Subject    :", em.header.make_header(em.header.decode_header(m["Subject"])))
print("  parts      :", [p.get_content_type() for p in m.walk()])
assert m["Message-ID"] == "<CAF=orig123@mail.gmail.com>"
assert m["In-Reply-To"] == "<prev999@mail.gmail.com>"

print("== 3. webhook replay is idempotent ==")
r = post(ev); assert r.status_code == 200
assert len(list(pathlib.Path(MAILDIR).joinpath("new").iterdir())) == 1
print("  still 1 file, meta hits:", HITS["meta"], "raw hits:", HITS["raw"])

print("== 4. bad signature rejected ==")
r = client.post("/webhook", content=json.dumps(ev), headers={
    "svix-id":"msg_x","svix-timestamp":str(int(time.time())),
    "svix-signature":"v1,AAAA","content-type":"application/json"})
print("  status", r.status_code); assert r.status_code == 401

print("== 5. dedupe survives Dovecot new/ -> cur/:2,S rename ==")
p = new[0]; p.rename(pathlib.Path(MAILDIR)/"cur"/(p.name+":2,S"))
r = post(ev); assert r.status_code == 200
assert list(pathlib.Path(MAILDIR).joinpath("new").iterdir()) == []
print("  no duplicate re-delivered")

print("== 6. recipient allow-list ==")
r = post({"type":"email.received","data":{"email_id":"zzz","to":["billing@nelgixa.resend.app"]}})
print("  status", r.status_code, "| skipped_recipient =", main.STATS["skipped_recipient"])
assert main.STATS["skipped_recipient"] == 1

print("== 7. reconciler recovers a missed email (and fallback when raw is null) ==")
main.reconcile_once()
allf = list(pathlib.Path(MAILDIR).joinpath("new").iterdir())
print("  files delivered by reconcile:", [f.name.split('.')[1] for f in allf])
assert any(EMAIL_ID2 in f.name for f in allf), "fallback message not delivered"
fb = em.message_from_bytes([f for f in allf if EMAIL_ID2 in f.name][0].read_bytes())
print("  fallback Message-ID:", fb["Message-ID"], "| X-Custom:", fb["X-Custom"])
print("  fallback parts     :", [q.get_content_type() for q in fb.walk()])

print("== 8. healthz reports 503 while Dovecot is down ==")
r = client.get("/healthz")
print("  status", r.status_code, "(expect 503)")
assert r.status_code == 503
detail = r.json()["detail"]
print("  imap_ok:", detail["imap_ok"], "| delivered:", detail["delivered"])
assert detail["imap_ok"] is False

print("== 9. healthz goes green only after successful IMAP authentication ==")
import socketserver, socket as _s
class FakeIMAP(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b"* OK [CAPABILITY IMAP4rev1] Dovecot ready.\r\n")
        try:
            while line := self.rfile.readline():
                tag, command, *_ = line.split()
                if command.upper() == b"CAPABILITY":
                    self.wfile.write(b"* CAPABILITY IMAP4rev1\r\n" + tag + b" OK capabilities\r\n")
                elif command.upper() == b"LOGIN" and b'bridge-test-password' in line and b'support' in line:
                    self.wfile.write(tag + b" OK logged in\r\n")
                elif command.upper() == b"LOGOUT":
                    self.wfile.write(b"* BYE closing\r\n" + tag + b" OK logout\r\n")
                    return
                else:
                    self.wfile.write(tag + b" NO authentication failed\r\n")
        except ConnectionResetError:
            pass
class Srv(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
imap_srv = Srv(("127.0.0.1", main.IMAP_PORT_TEST if hasattr(main,"IMAP_PORT_TEST") else main.IMAP_PORT), FakeIMAP)
threading.Thread(target=imap_srv.serve_forever, daemon=True).start()
time.sleep(0.3)
r = client.get("/healthz")
print("  status", r.status_code, "(expect 200)")
h = r.json()
print(" ", {k: h[k] for k in ("ok","imap_ok","mailboxes","unread_total","delivered","skipped_duplicate","skipped_recipient","errors")})
assert r.status_code == 200 and h["imap_ok"] is True

print("== 10. single-mailbox fallback lands in <root>/support ==")
print("  mailboxes:", h["mailboxes"], "| root:", h["maildir_root"])
assert h["mailboxes"] == ["support"]

print("\nALL TESTS PASSED")
