#!/bin/sh
# Renders dovecot.conf from environment variables, prepares the Maildir,
# then hands off to supervisord which runs Dovecot + the webhook service.
set -eu

: "${MAILBRIDGE_IMAP_USER:=support}"
: "${MAILBRIDGE_MAILDIR_ROOT:=/srv/mail}"
: "${MAILBRIDGE_IMAP_PORT:=143}"
: "${MAILBRIDGE_UID:=1000}"
: "${MAILBRIDGE_GID:=1000}"
: "${DOVECOT_CONF:=/etc/dovecot/dovecot.conf}"

if [ -z "${MAILBRIDGE_IMAP_PASSWORD:-}" ]; then
  echo "FATAL: MAILBRIDGE_IMAP_PASSWORD is not set." >&2
  echo "Generate one with: openssl rand -hex 24" >&2
  exit 1
fi

if [ -z "${RESEND_API_KEY:-}" ]; then
  echo "FATAL: RESEND_API_KEY is not set." >&2
  exit 1
fi

if [ -z "${RESEND_WEBHOOK_SECRET:-}" ]; then
  echo "WARNING: RESEND_WEBHOOK_SECRET is not set - webhook signatures will NOT" >&2
  echo "         be verified. Anyone who finds the URL can inject tickets." >&2
fi

# Work out the mailbox list. MAILBRIDGE_ROUTES ("addr=mailbox,addr=mailbox")
# wins; otherwise fall back to the single MAILBRIDGE_IMAP_USER mailbox.
MAILBOXES=$(
  {
    printf '%s\n' "${MAILBRIDGE_ROUTES:-}" | tr ',' '\n' | sed -n 's/.*=//p'
    printf '%s\n' "${MAILBRIDGE_DEFAULT_MAILBOX:-}"
  } | tr -d ' ' | grep -v '^$' | sort -u
)
[ -n "${MAILBOXES}" ] || MAILBOXES="${MAILBRIDGE_IMAP_USER}"

for box in ${MAILBOXES}; do
  case "${box}" in
    *[!a-z0-9._-]*|"")
      echo "FATAL: invalid mailbox name '${box}' in MAILBRIDGE_ROUTES." >&2
      echo "       Use lowercase letters, digits, dot, dash, underscore." >&2
      exit 1 ;;
  esac
  mkdir -p "${MAILBRIDGE_MAILDIR_ROOT}/${box}/tmp" \
           "${MAILBRIDGE_MAILDIR_ROOT}/${box}/new" \
           "${MAILBRIDGE_MAILDIR_ROOT}/${box}/cur"
done

# One IMAP account per mailbox, all sharing MAILBRIDGE_IMAP_PASSWORD. A
# passwd-file (rather than a static passdb) means only the mailboxes you
# configured can log in - a typo'd username is rejected instead of silently
# creating an empty mailbox.
: > /etc/dovecot/users
for box in ${MAILBOXES}; do
  printf '%s:{PLAIN}%s:%s:%s::%s/%s::\n' \
    "${box}" "${MAILBRIDGE_IMAP_PASSWORD}" \
    "${MAILBRIDGE_UID}" "${MAILBRIDGE_GID}" \
    "${MAILBRIDGE_MAILDIR_ROOT}" "${box}" >> /etc/dovecot/users
done
chmod 0600 /etc/dovecot/users
if [ "$(id -u)" = "0" ]; then
  chown -R "${MAILBRIDGE_UID}:${MAILBRIDGE_GID}" "${MAILBRIDGE_MAILDIR_ROOT}"
  chmod -R 0700 "${MAILBRIDGE_MAILDIR_ROOT}"
  chown root:root /etc/dovecot/users
else
  echo "WARNING: not running as root; skipping chown of ${MAILBRIDGE_MAILDIR_ROOT}." >&2
  echo "         Dovecot refuses to handle mail as root, so do not override 'user:'." >&2
fi

# Dovecot's static passdb wants the password inline, which is why this file is
# generated at boot rather than baked into the image or bind-mounted.
cat > "${DOVECOT_CONF}" <<CONF
protocols = imap
listen = *
log_path = /dev/stderr
info_log_path = /dev/stderr

mail_location = maildir:${MAILBRIDGE_MAILDIR_ROOT}/%u
mail_uid = ${MAILBRIDGE_UID}
mail_gid = ${MAILBRIDGE_GID}
first_valid_uid = ${MAILBRIDGE_UID}
last_valid_uid = ${MAILBRIDGE_UID}

# No TLS by design: this port is never published outside the Docker network.
ssl = no
disable_plaintext_auth = no
auth_mechanisms = plain login

namespace inbox {
  inbox = yes
  separator = /
}

passdb {
  driver = passwd-file
  args = username_format=%u /etc/dovecot/users
}

userdb {
  driver = passwd-file
  args = username_format=%u /etc/dovecot/users
  default_fields = uid=${MAILBRIDGE_UID} gid=${MAILBRIDGE_GID} home=${MAILBRIDGE_MAILDIR_ROOT}/%u
}

service imap-login {
  inet_listener imap {
    port = ${MAILBRIDGE_IMAP_PORT}
  }
  inet_listener imaps {
    port = 0
  }
}
CONF

chmod 0600 "${DOVECOT_CONF}"

# Resolve binaries through PATH. supervisord and uvicorn come from pip
# (/usr/local/bin), dovecot from apt (/usr/sbin) - hardcoding either is how
# you get "exec: not found" at boot.
SUPERVISORD_BIN="${SUPERVISORD_BIN:-$(command -v supervisord || true)}"
DOVECOT_BIN="${DOVECOT_BIN:-$(command -v dovecot || true)}"
UVICORN_BIN="${UVICORN_BIN:-$(command -v uvicorn || true)}"

missing=""
[ -x "${SUPERVISORD_BIN:-}" ] || missing="${missing} supervisord"
[ -x "${DOVECOT_BIN:-}" ]     || missing="${missing} dovecot"
[ -x "${UVICORN_BIN:-}" ]     || missing="${missing} uvicorn"
if [ -n "${missing}" ]; then
  echo "FATAL: missing executable(s):${missing}" >&2
  echo "       PATH=${PATH}" >&2
  echo "       The image is built wrong - rebuild from the bundled Dockerfile." >&2
  exit 1
fi
export DOVECOT_BIN UVICORN_BIN

echo "mailbridge: root=${MAILBRIDGE_MAILDIR_ROOT} imap_port=${MAILBRIDGE_IMAP_PORT}"
echo "mailbridge: mailboxes=$(echo ${MAILBOXES} | tr '\n' ' ')"
echo "mailbridge: supervisord=${SUPERVISORD_BIN} dovecot=${DOVECOT_BIN} uvicorn=${UVICORN_BIN}"

exec "${SUPERVISORD_BIN}" -c "${SUPERVISORD_CONF:-/etc/supervisord.conf}"
