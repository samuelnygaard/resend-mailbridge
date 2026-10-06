#!/bin/sh
# Renders dovecot.conf from environment variables, prepares the Maildir,
# then hands off to supervisord which runs Dovecot + the webhook service.
set -eu
set -f  # Never expand mailbox names as filesystem globs.
LC_ALL=C
export LC_ALL

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
  echo "WARNING: RESEND_WEBHOOK_SECRET is not set - /webhook returns 503 until" >&2
  echo "         signing is configured. IMAP and recovery remain available." >&2
fi

# Use the same recipient-derived accounts and legacy mappings as the service.
# Parsing happens before any filesystem changes, so invalid config fails safely.
MAILBOXES=$(python -c 'from main import MAILBOXES; print("\n".join(MAILBOXES))')
if [ -z "${MAILBOXES}" ]; then
  echo "FATAL: no mailboxes configured." >&2
  exit 1
fi
# Names are one per line. Preserve embedded spaces/tabs so validation rejects
# them instead of splitting one invalid name into several accepted accounts.
IFS='
'

for box in ${MAILBOXES}; do
  case "${box}" in
    [!a-z0-9]*|*[!a-z0-9._-]*|"")
      echo "FATAL: invalid mailbox name '${box}'." >&2
      echo "       Use lowercase letters, digits, dot, dash, underscore." >&2
      exit 1 ;;
  esac
  if [ "${#box}" -gt 64 ]; then
    echo "FATAL: mailbox name '${box}' exceeds 64 characters." >&2
    exit 1
  fi
done

# Validate the entire list before root creates any directories.
for box in ${MAILBOXES}; do
  mkdir -p "${MAILBRIDGE_MAILDIR_ROOT}/${box}/tmp" \
           "${MAILBRIDGE_MAILDIR_ROOT}/${box}/new" \
           "${MAILBRIDGE_MAILDIR_ROOT}/${box}/cur"
done

# One IMAP account per mailbox, all sharing MAILBRIDGE_IMAP_PASSWORD. A
# passwd-file (rather than a static passdb) means only the mailboxes you
# configured can log in - a typo'd username is rejected instead of silently
# creating an empty mailbox.
# Hash once for all accounts. Raw passwords may contain passwd-file delimiters.
PASSWORD_HASH=$(doveadm pw -s SHA512-CRYPT -p "${MAILBRIDGE_IMAP_PASSWORD}")
umask 0077
USERS_TMP=$(mktemp /etc/dovecot/users.XXXXXX)
trap 'rm -f "${USERS_TMP}"' EXIT HUP INT TERM
for box in ${MAILBOXES}; do
  printf '%s:%s:%s:%s::%s/%s::\n' \
    "${box}" "${PASSWORD_HASH}" \
    "${MAILBRIDGE_UID}" "${MAILBRIDGE_GID}" \
    "${MAILBRIDGE_MAILDIR_ROOT}" "${box}" >> "${USERS_TMP}"
done
# The unprivileged Dovecot auth process must be able to read its passwd-file.
chown root:dovecot "${USERS_TMP}"
chmod 0640 "${USERS_TMP}"
# Publish only after the authentication process can read the complete file.
mv -f "${USERS_TMP}" /etc/dovecot/users
trap - EXIT HUP INT TERM
if [ "$(id -u)" = "0" ]; then
  chown -R "${MAILBRIDGE_UID}:${MAILBRIDGE_GID}" "${MAILBRIDGE_MAILDIR_ROOT}"
  chmod -R 0700 "${MAILBRIDGE_MAILDIR_ROOT}"
else
  echo "WARNING: not running as root; skipping chown of ${MAILBRIDGE_MAILDIR_ROOT}." >&2
  echo "         Dovecot refuses to handle mail as root, so do not override 'user:'." >&2
fi

# Generate the Dovecot config at boot; credentials live only in the passwd-file.
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
