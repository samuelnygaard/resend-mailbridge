import tempfile, pathlib as _pl
APP_DIR = str(_pl.Path(__file__).resolve().parent.parent / "app")
TMP = tempfile.mkdtemp(prefix="mailbridge-test-")
import base64, json, os, threading, time, secrets, pathlib, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

def eml(to, mid):
    return (f"Message-ID: <{mid}>\r\nFrom: Jane <jane@cust.com>\r\nTo: {to}\r\n"
            f"Subject: hej\r\n\r\nbody\r\n").encode()

MAILS = {
    "id-sup": ("support@nelgixa.resend.app", eml("support@example.com", "sup@x")),
    "id-sal": ("sales@nelgixa.resend.app",   eml("sales@example.com", "sal@x")),
    "id-both":("support@nelgixa.resend.app", eml("support@example.com", "both@x")),
    "id-junk":("admin@nelgixa.resend.app",   eml("admin@example.com", "junk@x")),
}
class Mock(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def _j(self,o,c=200):
        b=json.dumps(o).encode(); self.send_response(c)
        self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        p=self.path.split("?")[0]
        if p.startswith("/emails/receiving/"):
            i=p.rsplit("/",1)[1]
            if i not in MAILS: return self._j({"error":"nf"},404)
            to=[MAILS[i][0]] if i!="id-both" else ["support@nelgixa.resend.app","sales@nelgixa.resend.app"]
            return self._j({"id":i,"to":to,"from":"jane@cust.com","subject":"hej",
                            "raw":{"download_url":f"http://127.0.0.1:{PORT}/raw/{i}"}})
        if p.startswith("/raw/"):
            i=p.rsplit("/",1)[1]; b=MAILS[i][1]
            self.send_response(200); self.send_header("Content-Length",str(len(b))); self.end_headers()
            return self.wfile.write(b)
        if p=="/emails/receiving":
            return self._j({"data":[{"id":k,"to":[v[0]]} for k,v in MAILS.items()]})
        self._j({"error":"nf"},404)

srv=HTTPServer(("127.0.0.1",0),Mock); PORT=srv.server_address[1]
threading.Thread(target=srv.serve_forever,daemon=True).start()

SECRET="whsec_"+base64.b64encode(secrets.token_bytes(24)).decode()
ROOT=TMP
os.environ.update(
  RESEND_API_BASE=f"http://127.0.0.1:{PORT}", RESEND_API_KEY="re_k", RESEND_WEBHOOK_SECRET=SECRET,
  MAILBRIDGE_MAILDIR_ROOT=ROOT, MAILBRIDGE_IMAP_PORT="14301", DISABLE_RECONCILER="1",
  MAILBRIDGE_ROUTES="support@nelgixa.resend.app=support,sales@nelgixa.resend.app=sales",
)
os.environ.pop("MAILDIR",None); os.environ.pop("ALLOWED_RECIPIENTS",None)
import sys; sys.path.insert(0,APP_DIR); import main
from fastapi.testclient import TestClient
from svix.webhooks import Webhook
# __enter__ runs the FastAPI startup event, which pre-creates every mailbox
_cm=TestClient(main.app); c=_cm.__enter__()

def post(eid,to):
    body=json.dumps({"type":"email.received","data":{"email_id":eid,"to":to}})
    mid,ts="msg_"+eid,int(time.time())
    sig=Webhook(SECRET).sign(mid,datetime.datetime.fromtimestamp(ts,datetime.timezone.utc),body)
    return c.post("/webhook",content=body,headers={"svix-id":mid,"svix-timestamp":str(ts),
                  "svix-signature":sig,"content-type":"application/json"})

def files(box): return sorted(p.name.split(".")[1] for p in pathlib.Path(ROOT,box,"new").iterdir())

print("== config ==");  print("  mailboxes:",main.MAILBOXES,"| default:",main.DEFAULT_MAILBOX or None)
print("== support@ routes to the support mailbox ==")
assert post("id-sup",["support@nelgixa.resend.app"]).status_code==200
print("  support:",files("support"),"| sales:",files("sales"))
assert files("support")==["id-sup"] and files("sales")==[]

print("== sales@ routes to the sales mailbox ==")
assert post("id-sal",["sales@nelgixa.resend.app"]).status_code==200
print("  support:",files("support"),"| sales:",files("sales"))
assert files("sales")==["id-sal"]

print("== addressed to BOTH -> one ticket, first route wins ==")
assert post("id-both",["support@nelgixa.resend.app","sales@nelgixa.resend.app"]).status_code==200
print("  support:",files("support"),"| sales:",files("sales"))
assert files("support")==["id-both","id-sup"] and files("sales")==["id-sal"], (files("support"),files("sales"))

print("== unrouted admin@ is dropped ==")
assert post("id-junk",["admin@nelgixa.resend.app"]).status_code==200
print("  skipped_recipient:",main.STATS["skipped_recipient"],"| support:",files("support"))
assert main.STATS["skipped_recipient"]==1

print("== reconciler respects routes and does not duplicate ==")
main.reconcile_once()
print("  support:",files("support"),"| sales:",files("sales"))
assert files("support")==["id-both","id-sup"] and files("sales")==["id-sal"]

print("== healthz reports per-mailbox ==")
h=c.get("/healthz")
d=h.json() if h.status_code==200 else h.json()["detail"]
print("  routes      :",d["routes"])
print("  unread      :",d["unread_in_new"],"total",d["unread_total"])
print("  per_mailbox :",d["per_mailbox"])
assert d["per_mailbox"]=={"support":2,"sales":1}

print("== bad mailbox name is rejected at startup ==")
import importlib
os.environ["MAILBRIDGE_ROUTES"]="a@b.com=../escape"
try:
    importlib.reload(main); print("  FAILED: accepted"); raise SystemExit(1)
except ValueError as e: print("  rejected:",e)

print("\nALL ROUTING TESTS PASSED")
