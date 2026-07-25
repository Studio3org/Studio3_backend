#!/usr/bin/env bash
# Deploy app code from this laptop to EC2 and restart the API.
#
# Prerequisites:
#   - create-infra.sh completed (EC2_HOST, DATABASE_URL, REDIS_URL in config.env)
#   - bootstrap-ec2.sh run once on the instance
#   - SSH key path in DEPLOY_SSH_KEY
#
# Usage (repo root):
#   ./deploy/deploy.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="${ROOT}/deploy/config.env"

if [[ ! -f "$CONFIG" ]]; then
  echo "Missing $CONFIG"
  exit 1
fi
# shellcheck disable=SC1090
source "$CONFIG"

: "${EC2_HOST:?Run create-infra.sh first (EC2_HOST empty)}"
: "${DATABASE_URL:?DATABASE_URL missing in config.env}"
: "${REDIS_URL:?REDIS_URL missing in config.env}"
: "${DEPLOY_SSH_KEY:?}"
: "${JWT_SECRET:=}"
: "${SECRET_KEY:=}"

EC2_USER="${EC2_USER:-ec2-user}"
APP_DIR="/opt/studio3"
SSH_KEY="${DEPLOY_SSH_KEY/#\~/$HOME}"
SSH=(ssh -i "$SSH_KEY" -o StrictHostKeyChecking=accept-new "${EC2_USER}@${EC2_HOST}")
RSYNC=(rsync -az --delete
  --exclude '.git'
  --exclude '.venv'
  --exclude 'graphify-out'
  --exclude '.claude'
  --exclude '__pycache__'
  --exclude '*.pyc'
  --exclude '.env'
  --exclude '.env.*'
  --exclude 'deploy/config.env'
  --exclude 'deploy/infra-output.env'
  --exclude 'logs'
  --exclude 'uploads'
  --exclude '.DS_Store'
)

echo "==> Syncing code → ${EC2_USER}@${EC2_HOST}:${APP_DIR}"
"${RSYNC[@]}" -e "ssh -i ${SSH_KEY} -o StrictHostKeyChecking=accept-new" \
  "${ROOT}/" "${EC2_USER}@${EC2_HOST}:${APP_DIR}/"

# Generate secrets if not set
if [[ -z "${JWT_SECRET}" ]]; then
  JWT_SECRET=$(openssl rand -hex 32)
fi
if [[ -z "${SECRET_KEY}" ]]; then
  SECRET_KEY=$(openssl rand -hex 32)
fi

FRONTEND_URL="${FRONTEND_URL:-https://${DOMAIN:-localhost}}"
AWS_REGION="${AWS_REGION:-us-east-1}"

echo "==> Writing .env.production on server"
# Build env file locally then scp (avoids shell-escaping hell over SSH)
TMP_ENV=$(mktemp)
trap 'rm -f "$TMP_ENV"' EXIT
cat > "$TMP_ENV" <<EOF
FLASK_ENV=production
PORT=9000
DATABASE_URL=${DATABASE_URL}
REDIS_URL=${REDIS_URL}
JWT_SECRET=${JWT_SECRET}
SECRET_KEY=${SECRET_KEY}
JWT_ACCESS_EXPIRY_MINUTES=${JWT_ACCESS_EXPIRY_MINUTES:-15}
SALT_ROUNDS=${SALT_ROUNDS:-10}
FRONTEND_URL=${FRONTEND_URL}
AWS_REGION=${AWS_REGION}
AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID:-}
AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY:-}
S3_BUCKET=${S3_BUCKET:-}
S3_PUBLIC_BASE_URL=${S3_PUBLIC_BASE_URL:-}
SES_FROM_EMAIL=${SES_FROM_EMAIL:-}
FIREBASE_SERVICE_ACCOUNT_JSON=${FIREBASE_SERVICE_ACCOUNT_JSON:-}
STRIPE_SECRET_KEY=${STRIPE_SECRET_KEY:-}
EOF

scp -i "$SSH_KEY" -o StrictHostKeyChecking=accept-new \
  "$TMP_ENV" "${EC2_USER}@${EC2_HOST}:${APP_DIR}/.env.production"

# Persist generated secrets back into config.env so they stay stable across deploys
upsert_config() {
  local key="$1" val="$2"
  if grep -q "^${key}=" "$CONFIG" 2>/dev/null; then
    if sed --version >/dev/null 2>&1; then
      sed -i "s|^${key}=.*|${key}=${val}|" "$CONFIG"
    else
      sed -i '' "s|^${key}=.*|${key}=${val}|" "$CONFIG"
    fi
  else
    printf '\n%s=%s\n' "$key" "$val" >> "$CONFIG"
  fi
}
upsert_config JWT_SECRET "$JWT_SECRET"
upsert_config SECRET_KEY "$SECRET_KEY"

echo "==> Install deps + systemd + restart"
"${SSH[@]}" bash -s <<REMOTE
set -euo pipefail
cd ${APP_DIR}
mkdir -p logs
if [[ ! -d .venv ]]; then
  command -v python3.12 >/dev/null && PY=python3.12 || PY=python3
  \$PY -m venv .venv
fi
source .venv/bin/activate
pip install -q -r requirements.txt

sudo cp deploy/systemd/studio3-api.service /etc/systemd/system/studio3-api.service
sudo systemctl daemon-reload
sudo systemctl enable studio3-api
sudo systemctl restart studio3-api
sleep 2
sudo systemctl --no-pager --full status studio3-api || true
REMOTE

echo
echo "Deployed. Health check:"
echo "  curl -sS http://${EC2_HOST}/"
if [[ -n "${DOMAIN:-}" ]]; then
  echo "  After DNS + SSL: https://${DOMAIN}/"
fi
