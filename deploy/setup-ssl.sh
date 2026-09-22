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
# The config file is deliberately absent on the server: deploy.sh excludes it from the
# rsync because it holds every production secret, and only two non-secret values are
# needed here. So take them from the environment when the file is not there, which is
# the normal case for the host this script is meant to run on.
if [[ -f "$CONFIG" ]]; then
  # shellcheck disable=SC1090
  source "$CONFIG"
fi

if [[ -z "${DOMAIN:-}" || -z "${SSL_EMAIL:-}" ]]; then
  echo "DOMAIN and SSL_EMAIL are required." >&2
  echo >&2
  echo "On the server, pass them in — sudo -E keeps them through sudo:" >&2
  echo "  DOMAIN=api.example.com SSL_EMAIL=you@example.com \\" >&2
  echo "    sudo -E bash deploy/setup-ssl.sh" >&2
  echo >&2
  echo "On a laptop with deploy/config.env present, they are read from it." >&2
  exit 1
fi

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
