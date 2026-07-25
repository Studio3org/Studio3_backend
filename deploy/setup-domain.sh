#!/usr/bin/env bash
# Create a Route 53 hosted zone (if needed) and an A record → EC2 Elastic IP.
# Does NOT buy a domain — register at Route 53 / Namecheap / Google Domains first,
# then set DOMAIN in config.env and run this (or point DNS at your registrar).
#
# Usage:
#   ./deploy/setup-domain.sh
#
# If the domain is registered outside Route 53, this script still creates a
# hosted zone and prints NS records you must copy to your registrar.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="${ROOT}/deploy/config.env"
# shellcheck disable=SC1090
source "$CONFIG"

: "${AWS_REGION:?}"
: "${DOMAIN:?Set DOMAIN e.g. api.studiothree.com}"
: "${EC2_HOST:?Run create-infra.sh first}"

export AWS_DEFAULT_REGION="$AWS_REGION"
export AWS_PROFILE="${AWS_PROFILE:-default}"

# Parent zone = last two labels for common cases; for api.foo.com → foo.com
# Override with HOSTED_ZONE_NAME=foo.com in config.env if needed.
HOSTED_ZONE_NAME="${HOSTED_ZONE_NAME:-}"
if [[ -z "$HOSTED_ZONE_NAME" ]]; then
  # strip first label: api.example.com → example.com
  HOSTED_ZONE_NAME="${DOMAIN#*.}"
  if [[ "$HOSTED_ZONE_NAME" == "$DOMAIN" ]]; then
    HOSTED_ZONE_NAME="$DOMAIN"
  fi
fi

echo "API host:    $DOMAIN"
echo "Hosted zone: $HOSTED_ZONE_NAME"
echo "Target IP:   $EC2_HOST"

ZONE_ID=$(aws route53 list-hosted-zones-by-name --dns-name "${HOSTED_ZONE_NAME}." \
  --query "HostedZones[?Name=='${HOSTED_ZONE_NAME}.'].Id" --output text | head -1 | sed 's|/hostedzone/||')

if [[ -z "$ZONE_ID" || "$ZONE_ID" == "None" ]]; then
  echo "==> Creating hosted zone for ${HOSTED_ZONE_NAME}"
  CALLER=$(date +%s)
  ZONE_ID=$(aws route53 create-hosted-zone \
    --name "$HOSTED_ZONE_NAME" \
    --caller-reference "studio3-${CALLER}" \
    --query 'HostedZone.Id' --output text | sed 's|/hostedzone/||')
  echo "Created zone: $ZONE_ID"
  echo
  echo "IMPORTANT — set these nameservers at your domain registrar:"
  aws route53 get-hosted-zone --id "$ZONE_ID" --query 'DelegationSet.NameServers' --output table
  echo
  echo "Wait until NS propagate (can take minutes to hours), then re-run or continue."
else
  echo "Using existing hosted zone: $ZONE_ID"
fi

CHANGE_BATCH=$(cat <<EOF
{
  "Comment": "studio3 API A record",
  "Changes": [{
    "Action": "UPSERT",
    "ResourceRecordSet": {
      "Name": "${DOMAIN}",
      "Type": "A",
      "TTL": 60,
      "ResourceRecords": [{"Value": "${EC2_HOST}"}]
    }
  }]
}
EOF
)

echo "==> UPSERT A ${DOMAIN} → ${EC2_HOST}"
aws route53 change-resource-record-sets \
  --hosted-zone-id "$ZONE_ID" \
  --change-batch "$CHANGE_BATCH" >/dev/null

echo "Done. Check: dig +short ${DOMAIN}"
echo "When it returns ${EC2_HOST}, run on EC2: bash /opt/studio3/deploy/setup-ssl.sh"
