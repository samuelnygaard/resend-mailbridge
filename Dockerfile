# mailbridge - Resend inbound webhooks -> Maildir -> Dovecot IMAP, in one image.
#
# Debian bookworm ships Dovecot 2.3.x, which is what the generated config
# targets. Dovecot 2.4 rewrote the config format, so do not casually move to a
# base image that carries it.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MAILBRIDGE_HTTP_PORT=8080 \
    MAILBRIDGE_IMAP_PORT=143 \
    MAILBRIDGE_IMAP_USER=support \
    MAILBRIDGE_MAILDIR_ROOT=/srv/mail \
    MAILBRIDGE_UID=1000 \
    MAILBRIDGE_GID=1000

RUN apt-get update \
 && apt-get install -y --no-install-recommends dovecot-imapd ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd -g 1000 mailbridge \
 && useradd -u 1000 -g 1000 -M -s /usr/sbin/nologin mailbridge \
 && mkdir -p /srv/mail

WORKDIR /app
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/main.py .

# Fail the BUILD, not the boot, if any of the three binaries is missing.
RUN set -eux; command -v supervisord; command -v uvicorn; command -v dovecot
COPY supervisord.conf /etc/supervisord.conf
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 8080 143
HEALTHCHECK --interval=30s --timeout=10s --start-period=25s --retries=5 \
  CMD python -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ['MAILBRIDGE_HTTP_PORT']+'/healthz',timeout=5).status==200 else 1)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
