#!/usr/bin/env bash
# First-time setup ON the EC2 instance (Amazon Linux 2023).
# Installs Python 3.12, nginx, redis tools, clones nothing — deploy.sh syncs code.
#
# Usage (on EC2 as ec2-user, after create-infra):
#   curl -sSL ...  OR copy this file up, then:
#   bash bootstrap-ec2.sh
#
# Or from laptop after infra:
#   scp -i $KEY deploy/bootstrap-ec2.sh ec2-user@$EC2_HOST:~/
#   ssh -i $KEY ec2-user@$EC2_HOST 'bash bootstrap-ec2.sh'

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/studio3}"
APP_USER="${APP_USER:-ec2-user}"

echo "==> Installing system packages (Amazon Linux 2023)"
sudo dnf update -y
sudo dnf install -y git nginx gcc openssl-devel libffi-devel

# Prefer Python 3.12 (matches runtime.txt); fall back to 3.11
PY=""
if sudo dnf install -y python3.12 python3.12-pip python3.12-devel 2>/dev/null; then
  PY=python3.12
elif sudo dnf install -y python3.11 python3.11-pip python3.11-devel 2>/dev/null; then
  PY=python3.11
  echo "WARN: python3.12 not available — using 3.11"
else
  sudo dnf install -y python3 python3-pip python3-devel
  PY=python3
  echo "WARN: using default $($PY --version)"
fi

sudo dnf install -y postgresql15 2>/dev/null || sudo dnf install -y postgresql 2>/dev/null || true

# certbot
if ! command -v certbot >/dev/null 2>&1; then
  sudo dnf install -y certbot python3-certbot-nginx 2>/dev/null \
    || sudo "$PY" -m pip install certbot certbot-nginx
fi

echo "==> App directory ${APP_DIR}"
sudo mkdir -p "$APP_DIR"
sudo chown "${APP_USER}:${APP_USER}" "$APP_DIR"

if [[ ! -d "${APP_DIR}/.venv" ]]; then
  "$PY" -m venv "${APP_DIR}/.venv"
fi
# shellcheck disable=SC1091
source "${APP_DIR}/.venv/bin/activate"
pip install --upgrade pip wheel

echo "==> nginx site placeholder (SSL filled by setup-ssl.sh)"
sudo mkdir -p /etc/nginx/conf.d
if [[ ! -f /etc/nginx/conf.d/studio3.conf ]]; then
  sudo tee /etc/nginx/conf.d/studio3.conf >/dev/null <<'NGINX'
# Temporary HTTP reverse proxy — setup-ssl.sh upgrades to HTTPS
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;

    client_max_body_size 25m;

    location / {
        proxy_pass http://127.0.0.1:9000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 86400;
    }
}
NGINX
fi

sudo systemctl enable nginx
sudo systemctl restart nginx

echo "==> Done ($PY). Next: run deploy/deploy.sh from your laptop, then setup-ssl.sh on EC2."
