#!/usr/bin/env bash
# Deploy app code from this laptop to EC2 and restart the three services:
# the API (gunicorn), the Celery worker, and Celery beat.
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
# A value containing shell syntax — the Firebase service-account JSON is the one
# that bites — fails here as "command not found" naming a fragment of the value.
# Say what it actually means, since the message alone points nowhere useful.
# Checked on stderr rather than the exit status, because the failure that matters here
# does not fail. An unquoted value containing a space — CORS_ORIGINS with two origins,
# say — assigns the first word and runs the rest as a command: bash complains, source
# still returns 0, and the deploy proceeds with a silently truncated value.
CONFIG_ERR=$(source "$CONFIG" 2>&1 >/dev/null)
if [[ -n "$CONFIG_ERR" ]]; then
  echo "$CONFIG could not be read cleanly:" >&2
  echo >&2
  echo "$CONFIG_ERR" >&2
  echo >&2
  echo "This file is sourced by bash, so any value containing spaces, commas," >&2
  echo "braces or quotes must be wrapped in single quotes:" >&2
  echo "  CORS_ORIGINS='https://a.example.com,https://b.example.com'" >&2
  echo >&2
  echo "Refusing to deploy — the value would arrive truncated." >&2
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

# Everything src/shared/config/settings.py marks required in production. Checked here so a
# missing value costs a one-line error instead of a synced deploy that crash-loops on boot.
MISSING=()
# PLATFORM_COMMISSION_BPS is in this list rather than carrying a default: the two
# plausible values differ by half the artist's earnings, and a silent 20% because
# somebody left it blank is not a mistake that announces itself.
for var in FRONTEND_URL STRIPE_SECRET_KEY STRIPE_WEBHOOK_SECRET \
           PLATFORM_COMMISSION_BPS \
           AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY S3_BUCKET S3_PUBLIC_BASE_URL; do
  [[ -z "${!var:-}" ]] && MISSING+=("$var")
done
if [[ ${#MISSING[@]} -gt 0 ]]; then
  echo "Required in production but empty in deploy/config.env:"
  printf '  - %s\n' "${MISSING[@]}"
  echo
  echo "The service refuses to boot without them (src/shared/config/settings.py)."
  echo "Fill them in, or set ALLOW_INCOMPLETE=1 to deploy anyway."
  [[ "${ALLOW_INCOMPLETE:-}" == "1" ]] || exit 1
fi

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

echo "==> Syncing code -> ${EC2_USER}@${EC2_HOST}:${APP_DIR}"
"${RSYNC[@]}" -e "ssh -i ${SSH_KEY} -o StrictHostKeyChecking=accept-new" \
  "${ROOT}/" "${EC2_USER}@${EC2_HOST}:${APP_DIR}/"

# Generated on the first deploy only. upsert_config further down writes them back
# into config.env, so the next deploy reads them from there rather than minting new
# ones — which would sign out every user and end every admin session.
if [[ -z "${JWT_SECRET}" ]]; then
  JWT_SECRET=$(openssl rand -hex 32)
fi
if [[ -z "${SECRET_KEY}" ]]; then
  SECRET_KEY=$(openssl rand -hex 32)
fi

FRONTEND_URL="${FRONTEND_URL:-https://${DOMAIN:-localhost}}"
BACKEND_URL="${BACKEND_URL:-https://${DOMAIN:-$EC2_HOST}}"
AWS_REGION="${AWS_REGION:-us-east-1}"

echo "==> Writing .env.production on server"
# Build env file locally then scp (avoids shell-escaping hell over SSH).
# Mirrors .env.example; the contract itself is src/shared/config/settings.py.
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
CORS_ORIGINS='${CORS_ORIGINS:-}'
BACKEND_URL=${BACKEND_URL}

STRIPE_SECRET_KEY=${STRIPE_SECRET_KEY:-}
STRIPE_WEBHOOK_SECRET=${STRIPE_WEBHOOK_SECRET:-}
PLATFORM_CURRENCY=${PLATFORM_CURRENCY:-usd}
PLATFORM_COMMISSION_BPS=${PLATFORM_COMMISSION_BPS}
PLATFORM_TICKET_COMMISSION_BPS=${PLATFORM_TICKET_COMMISSION_BPS:-800}
CONNECT_ACCOUNT_COUNTRY=${CONNECT_ACCOUNT_COUNTRY:-US}
CONNECT_ONBOARDING_BASE_URL=${CONNECT_ONBOARDING_BASE_URL:-https://studio-3.co/connect}

AWS_REGION=${AWS_REGION}
AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID:-}
AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY:-}
S3_BUCKET=${S3_BUCKET:-}
S3_PUBLIC_BASE_URL=${S3_PUBLIC_BASE_URL:-}
LOCAL_MEDIA_DIR=${LOCAL_MEDIA_DIR:-}

SES_FROM_EMAIL=${SES_FROM_EMAIL:-}
# Single-quoted: this JSON contains spaces ("-----BEGIN PRIVATE KEY-----"), and an
# unquoted value in a systemd EnvironmentFile can truncate at the first one — which
# would break push notifications with nothing in the logs to say why.
FIREBASE_SERVICE_ACCOUNT_JSON='${FIREBASE_SERVICE_ACCOUNT_JSON:-}'

CELERY_BROKER_URL=${CELERY_BROKER_URL:-}
CELERY_REDIS_DB=${CELERY_REDIS_DB:-1}

APP_STORE_URL=${APP_STORE_URL:-}
PLAY_STORE_URL=${PLAY_STORE_URL:-}
IOS_TEAM_ID=${IOS_TEAM_ID:-}
IOS_BUNDLE_ID=${IOS_BUNDLE_ID:-com.studio3.discover}
ANDROID_PACKAGE=${ANDROID_PACKAGE:-com.studio3.discover}
ANDROID_CERT_FINGERPRINTS=${ANDROID_CERT_FINGERPRINTS:-}

# Creates the first admin if no account with this email exists yet. Without it a new
# environment has nobody who can reach the ops console, and no way to make one.
ADMIN_EMAIL=${ADMIN_EMAIL:-}
ADMIN_PASSWORD='${ADMIN_PASSWORD:-}'

SENTRY_DSN=${SENTRY_DSN:-}
EOF

scp -i "$SSH_KEY" -o StrictHostKeyChecking=accept-new \
  "$TMP_ENV" "${EC2_USER}@${EC2_HOST}:${APP_DIR}/.env.production"
"${SSH[@]}" "chmod 600 ${APP_DIR}/.env.production"

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

echo "==> Install deps + systemd units + restart api, worker, beat"
"${SSH[@]}" bash -s <<'REMOTE'
set -euo pipefail
APP_DIR=/opt/studio3
cd "$APP_DIR"
mkdir -p logs
if [[ ! -d .venv ]]; then
  command -v python3.12 >/dev/null && PY=python3.12 || PY=python3
  $PY -m venv .venv
fi
source .venv/bin/activate
pip install -q -r requirements.txt

for unit in studio3-api studio3-worker studio3-beat; do
  sudo cp "deploy/systemd/${unit}.service" "/etc/systemd/system/${unit}.service"
done
sudo systemctl daemon-reload

# API first: its ExecStartPre runs `alembic upgrade head`, and the job processes must not
# start against a schema the migration has not reached yet.
sudo systemctl enable --now studio3-api
sudo systemctl restart studio3-api
sudo systemctl enable --now studio3-worker
sudo systemctl restart studio3-worker
sudo systemctl enable --now studio3-beat
sudo systemctl restart studio3-beat

# After the API, because that unit's ExecStartPre is what runs the migrations and the
# users table has to exist. A no-op when ADMIN_EMAIL/ADMIN_PASSWORD are unset, and a
# no-op on every deploy after the first.
sudo -u ec2-user bash -lc 'cd /opt/studio3 && \
  FLASK_ENV=production .venv/bin/python scripts/bootstrap_admin.py' || \
  echo "!! admin bootstrap failed — the app is still up; see the message above"

# Watchdog for the failure systemd cannot see: beat running but no longer
# scheduling. It is a timer, not a service, so it is enabled separately.
sudo cp deploy/systemd/studio3-beat-watchdog.service /etc/systemd/system/
sudo cp deploy/systemd/studio3-beat-watchdog.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now studio3-beat-watchdog.timer

sleep 3
for unit in studio3-api studio3-worker studio3-beat; do
  printf '\n--- %s: %s ---\n' "$unit" "$(systemctl is-active "$unit")"
  sudo journalctl -u "$unit" -n 8 --no-pager || true
done
REMOTE

# Report against whatever is actually configured, and check rather than remind. The
# closing text used to print the raw IP and tell you to point Stripe at the domain on
# every single run, long after both were done — advice that is always shown is advice
# nobody reads.
BASE="${BACKEND_URL:-http://${EC2_HOST}}"

echo
echo "Deployed. Checking ${BASE} ..."
live=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 "${BASE}/" || echo "000")
ready=$(curl -sS --max-time 20 "${BASE}/health" 2>/dev/null || echo "")

if [[ "$live" == "200" ]]; then
  echo "  liveness  200"
else
  echo "  liveness  ${live}  <- the service is not answering; journalctl -u studio3-api"
fi

case "$ready" in
  *'"status":"ok"'*) echo "  readiness ok — database, redis, s3 and stripe all reachable" ;;
  "")               echo "  readiness no response from /health" ;;
  *)                echo "  readiness DEGRADED:"; echo "    $ready" ;;
esac

# Only worth saying when it is still true.
if [[ "$BASE" == http://* ]]; then
  echo
  echo "Still on plain HTTP. iOS and the browser both refuse it, and Stripe requires"
  echo "HTTPS for Connect return URLs — run deploy/setup-ssl.sh on the server."
fi
