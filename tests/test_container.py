"""Real Dovecot smoke test. Run after building the image; no Resend account needed."""

import argparse
import subprocess
import uuid


PROBE = r'''
import imaplib
import json
import os
import grp
import pwd
import stat
import time
import urllib.request

deadline = time.monotonic() + 30
while True:
    try:
        with urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3) as response:
            health = json.load(response)
        break
    except Exception:
        if time.monotonic() >= deadline:
            raise
        time.sleep(0.25)
assert health['mailboxes'] == ['sales', 'support'], health
password = os.environ['MAILBRIDGE_IMAP_PASSWORD']

users = os.stat('/etc/dovecot/users')
assert users.st_uid == 0 and users.st_gid == grp.getgrnam('dovecot').gr_gid
assert stat.S_IMODE(users.st_mode) == 0o640
# The auth process must read the hash, while the webhook uid must not.
for uid, gid, allowed in [(pwd.getpwnam('dovecot').pw_uid, grp.getgrnam('dovecot').gr_gid, True),
                          (1000, 1000, False)]:
    pid = os.fork()
    if pid == 0:
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)
        try:
            with open('/etc/dovecot/users', 'rb') as file:
                file.read()
            os._exit(0 if allowed else 1)
        except PermissionError:
            os._exit(1 if allowed else 0)
    assert os.waitpid(pid, 0)[1] == 0, 'passwd-file access is incorrect'

# Accounts must work before any messages have arrived.
for user in ['support', 'sales']:
    with imaplib.IMAP4('127.0.0.1', 143, timeout=10) as client:
        assert client.login(user, password)[0] == 'OK'
        assert client.select('INBOX')[1] == [b'0']

def message(user):
    return (f'From: Jane <jane@customer.example>\r\nTo: {user}@nelgixa.resend.app\r\n'
            f'Message-ID: <smoke-{user}@customer.example>\r\nSubject: {user} only\r\n'
            f'\r\n{user} only\r\n').encode()

# Use the webhook service's uid when writing mail, just as in production.
pid = os.fork()
if pid == 0:
    try:
        os.setgid(1000)
        os.setuid(1000)
        import main
        for user in ['support', 'sales']:
            box = main.mailbox_for({'to': [f'{user}@nelgixa.resend.app']})
            assert box == user
            assert main.deliver(message(user), f'smoke-{user}', box)
        os._exit(0)
    except Exception:
        import traceback
        traceback.print_exc()
        os._exit(1)
assert os.waitpid(pid, 0)[1] == 0

for user in ['support', 'sales']:
    with imaplib.IMAP4('127.0.0.1', 143, timeout=10) as client:
        assert client.login(user, password)[0] == 'OK'
        assert client.select('INBOX')[1] == [b'1']
        status, ids = client.search(None, 'ALL')
        assert status == 'OK' and len(ids[0].split()) == 1
        status, parts = client.fetch(ids[0], '(RFC822)')
        raw = next(part[1] for part in parts if isinstance(part, tuple))
        assert status == 'OK' and raw == message(user)
        print(user, 'login, isolated mailbox and original bytes: OK')

for user, secret in [('unknown', password), ('../support', password), ('support', 'wrong-password')]:
    # Dovecot progressively delays failed logins from the same client IP.
    with imaplib.IMAP4('127.0.0.1', 143, timeout=30) as client:
        try:
            client.login(user, secret)
        except imaplib.IMAP4.error:
            continue
        raise AssertionError('Invalid credentials accepted: ' + user)
print('Unknown users, path traversal and wrong passwords rejected: OK')
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="mailbridge:test")
    parser.add_argument("--docker", default="docker")
    args = parser.parse_args()
    name = "mailbridge-smoke-" + uuid.uuid4().hex[:12]

    def docker(*command, **options):
        return subprocess.run([args.docker, *command], check=True, **options)

    try:
        docker(
            "run", "-d", "--name", name,
            "-e", "RESEND_API_KEY=smoke-dummy",
            "-e", "RESEND_WEBHOOK_SECRET=whsec_bWFpbGJyaWRnZS1zbW9rZS10ZXN0",
            "-e", "MAILBRIDGE_IMAP_PASSWORD=smoke:only-password",
            "-e", "MAILBRIDGE_RECIPIENTS=support@nelgixa.resend.app,sales@nelgixa.resend.app",
            "-e", "DISABLE_RECONCILER=1", args.image,
        )
        docker("exec", name, "doveconf", "-n", stdout=subprocess.DEVNULL)
        docker("exec", "-i", name, "python", "-", input=PROBE, text=True)
        # Boot must reject unsafe legacy names before creating any Maildir.
        for route in ["a@example.com=..", "a@example.com=" + "a" * 65]:
            docker(
                "run", "--rm", "--entrypoint", "sh",
                "-e", "RESEND_API_KEY=smoke-dummy",
                "-e", "MAILBRIDGE_IMAP_PASSWORD=smoke-only-password",
                "-e", "MAILBRIDGE_MAILDIR_ROOT=/tmp/rejected-mail",
                "-e", "MAILBRIDGE_ROUTES=" + route, args.image, "-c",
                '/usr/local/bin/entrypoint.sh; status=$?; test "$status" -ne 0 && test ! -e /tmp/rejected-mail',
            )
        print("CONTAINER SMOKE TEST PASSED")
    except Exception:
        subprocess.run([args.docker, "logs", name], check=False)
        raise
    finally:
        subprocess.run([args.docker, "rm", "-f", name], check=False, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
