#!/usr/bin/env bash
# Issue Let's Encrypt cert and install HTTPS nginx config.
# Run ON EC2 after DOMAIN DNS A record points at this instance's Elastic IP.
#
# Usage on EC2:
#   cd /opt/studio3 && bash deploy/setup-ssl.sh
#
# Or from laptop:
#   ssh -i $KEY ec2-user@$HOST 'cd /opt/studio3 && bash deploy/setup-ssl.sh'

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# Prefer local config on laptop; on EC2 use /opt/studio3/deploy/config.env if synced
CONFIG="${ROOT}/deploy/config.env"
if [[ ! -f "$CONFIG" ]]; then
  echo "Missing deploy/config.env (DOMAIN and SSL_EMAIL required)."
  exit 1
fi
# shellcheck disable=SC1090
source "$CONFIG"

: "${DOMAIN:?Set DOMAIN in deploy/config.env (e.g. api.yourbrand.com)}"
: "${SSL_EMAIL:?Set SSL_EMAIL in deploy/config.env}"

echo "==> Domain: $DOMAIN"

sudo mkdir -p /var/www/certbot
# HTTP-only server for ACME challenge first
sudo tee /etc/nginx/conf.d/studio3.conf >/dev/null <<NGINX
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        proxy_pass http://127.0.0.1:9000;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 86400;
    }
}
NGINX

sudo nginx -t
sudo systemctl reload nginx

echo "==> Requesting certificate (certbot)"
sudo certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "$SSL_EMAIL" --redirect

# Ensure Socket.IO upgrade headers survive certbot's rewrite — re-apply our template if needed
if [[ -f "${ROOT}/deploy/nginx/studio3.conf" ]]; then
  TMP=$(mktemp)
  sed "s/DOMAIN_PLACEHOLDER/${DOMAIN}/g" "${ROOT}/deploy/nginx/studio3.conf" > "$TMP"
  # Only overwrite if certs exist
  if [[ -f "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem" ]]; then
    # certbot may have already written a working config; leave it if SSL works.
    # Install our WebSocket-friendly config only when ssl_dhparam exists.
    if [[ -f /etc/letsencrypt/ssl-dhparams.pem ]]; then
      sudo cp "$TMP" /etc/nginx/conf.d/studio3.conf
      sudo nginx -t && sudo systemctl reload nginx
    fi
  fi
  rm -f "$TMP"
fi

echo "HTTPS ready: https://${DOMAIN}/"
echo "Renewal is handled by certbot timer/cron on Amazon Linux."
