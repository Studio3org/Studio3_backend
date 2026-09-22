#!/usr/bin/env bash
# Create VPC networking pieces (default VPC), security groups, RDS Postgres,
# ElastiCache Redis, EC2 + Elastic IP for Studiothree backend.
#
# Prerequisites:
#   - AWS CLI v2 configured (aws configure / AWS_PROFILE)
#   - Key pair already created in the target region
#   - deploy/config.env filled (from config.env.example)
#
# Usage (from repo root):
#   cp deploy/config.env.example deploy/config.env   # edit values
#   ./deploy/create-infra.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="${ROOT}/deploy/config.env"

if [[ ! -f "$CONFIG" ]]; then
  echo "Missing $CONFIG — copy deploy/config.env.example and fill it in."
  exit 1
fi
# shellcheck disable=SC1090
source "$CONFIG"

: "${AWS_REGION:?}"
: "${PROJECT:?}"
: "${EC2_KEY_NAME:?}"
: "${SSH_CIDR:?Set SSH_CIDR to your public IP/32}"
: "${RDS_PASSWORD:?}"
: "${RDS_DB_NAME:?}"
: "${RDS_USERNAME:?}"

export AWS_DEFAULT_REGION="$AWS_REGION"
export AWS_PROFILE="${AWS_PROFILE:-default}"

NAME="${PROJECT}"
TAG_ARGS=(--tags "Key=Project,Value=${PROJECT}" "Key=ManagedBy,Value=create-infra.sh")

echo "==> Region: $AWS_REGION  Project: $PROJECT  Profile: $AWS_PROFILE"

# --- Default VPC + subnets (2 AZs for ElastiCache subnet group) ---
VPC_ID=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true \
  --query 'Vpcs[0].VpcId' --output text)
if [[ -z "$VPC_ID" || "$VPC_ID" == "None" ]]; then
  echo "No default VPC in $AWS_REGION. Create one in the VPC console, or extend this script."
  exit 1
fi
echo "VPC: $VPC_ID"

SUBNET_LIST=$(aws ec2 describe-subnets \
  --filters "Name=vpc-id,Values=${VPC_ID}" "Name=default-for-az,Values=true" \
  --query 'sort_by(Subnets,&AvailabilityZone)[].SubnetId' --output text)
if [[ -z "$SUBNET_LIST" || "$SUBNET_LIST" == "None" ]]; then
  SUBNET_LIST=$(aws ec2 describe-subnets \
    --filters "Name=vpc-id,Values=${VPC_ID}" \
    --query 'sort_by(Subnets,&AvailabilityZone)[].SubnetId' --output text)
fi
# shellcheck disable=SC2206
SUBNETS=($SUBNET_LIST)
if [[ ${#SUBNETS[@]} -lt 2 ]]; then
  echo "Need at least 2 subnets in different AZs for ElastiCache. Found: ${#SUBNETS[@]}"
  exit 1
fi
SUBNET_A="${SUBNETS[0]}"
SUBNET_B="${SUBNETS[1]}"
echo "Subnets: $SUBNET_A , $SUBNET_B"

# --- Security groups ---
sg_id() {
  local name="$1"
  aws ec2 describe-security-groups \
    --filters "Name=group-name,Values=${name}" "Name=vpc-id,Values=${VPC_ID}" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || true
}

ensure_sg() {
  local name="$1" desc="$2"
  local id
  id=$(sg_id "$name")
  if [[ -n "$id" && "$id" != "None" ]]; then
    echo "$id"
    return
  fi
  aws ec2 create-security-group \
    --group-name "$name" \
    --description "$desc" \
    --vpc-id "$VPC_ID" \
    --query 'GroupId' --output text
}

EC2_SG=$(ensure_sg "${NAME}-ec2-sg" "${NAME} API EC2")
RDS_SG=$(ensure_sg "${NAME}-rds-sg" "${NAME} Postgres RDS")
REDIS_SG=$(ensure_sg "${NAME}-redis-sg" "${NAME} ElastiCache Redis")
echo "SGs: EC2=$EC2_SG RDS=$RDS_SG REDIS=$REDIS_SG"

# Tag SGs (best-effort)
for sg in "$EC2_SG" "$RDS_SG" "$REDIS_SG"; do
  aws ec2 create-tags --resources "$sg" "${TAG_ARGS[@]}" 2>/dev/null || true
done

authorize_once() {
  # ignore Duplicate errors
  aws ec2 authorize-security-group-ingress "$@" 2>&1 | grep -v Duplicate || true
}

# EC2: SSH from you, HTTP/HTTPS public, app port only from itself (nginx fronts it)
authorize_once --group-id "$EC2_SG" --protocol tcp --port 22 --cidr "$SSH_CIDR"
authorize_once --group-id "$EC2_SG" --protocol tcp --port 80 --cidr 0.0.0.0/0
authorize_once --group-id "$EC2_SG" --protocol tcp --port 443 --cidr 0.0.0.0/0

# RDS: 5432 from EC2 SG only
authorize_once --group-id "$RDS_SG" --protocol tcp --port 5432 --source-group "$EC2_SG"

# Redis: 6379 from EC2 SG only
authorize_once --group-id "$REDIS_SG" --protocol tcp --port 6379 --source-group "$EC2_SG"

# --- AMI ---
AMI_ID="${EC2_AMI_ID:-}"
if [[ -z "$AMI_ID" ]]; then
  # SSM's public parameter is the canonical pointer to the current AL2023 image, but
  # reading it needs ssm:GetParameters — a permission a deploy user has no other reason
  # to hold. Fall back to asking EC2 directly, which AmazonEC2FullAccess already covers,
  # so the IAM setup does not have to grow a policy for one lookup.
  AMI_ID=$(aws ssm get-parameters \
    --names /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
    --query 'Parameters[0].Value' --output text 2>/dev/null || true)
  if [[ -z "$AMI_ID" || "$AMI_ID" == "None" ]]; then
    echo "==> SSM lookup unavailable; asking EC2 for the newest AL2023 image"
    AMI_ID=$(aws ec2 describe-images --owners amazon \
      --filters "Name=name,Values=al2023-ami-2023.*-kernel-6.1-x86_64" \
                "Name=state,Values=available" \
      --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text)
  fi
fi
if [[ -z "$AMI_ID" || "$AMI_ID" == "None" ]]; then
  echo "Could not resolve an AMI. Set EC2_AMI_ID in deploy/config.env." >&2
  exit 1
fi
echo "AMI: $AMI_ID"

# --- EC2 ---
INSTANCE_ID=$(aws ec2 describe-instances \
  --filters "Name=tag:Name,Values=${NAME}-api" "Name=instance-state-name,Values=pending,running,stopping,stopped" \
  --query 'Reservations[0].Instances[0].InstanceId' --output text 2>/dev/null || true)

if [[ -z "$INSTANCE_ID" || "$INSTANCE_ID" == "None" ]]; then
  echo "==> Launching EC2 ${EC2_INSTANCE_TYPE}..."
  INSTANCE_ID=$(aws ec2 run-instances \
    --image-id "$AMI_ID" \
    --instance-type "${EC2_INSTANCE_TYPE}" \
    --key-name "$EC2_KEY_NAME" \
    --security-group-ids "$EC2_SG" \
    --subnet-id "$SUBNET_A" \
    --associate-public-ip-address \
    --block-device-mappings '[{"DeviceName":"/dev/xvda","Ebs":{"VolumeSize":30,"VolumeType":"gp3","DeleteOnTermination":true}}]' \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=${NAME}-api},{Key=Project,Value=${PROJECT}}]" \
    --query 'Instances[0].InstanceId' --output text)
  echo "Waiting for instance $INSTANCE_ID..."
  aws ec2 wait instance-running --instance-ids "$INSTANCE_ID"
else
  echo "Reusing EC2: $INSTANCE_ID"
fi

# Elastic IP
ALLOC_ID=$(aws ec2 describe-addresses \
  --filters "Name=tag:Name,Values=${NAME}-api-eip" \
  --query 'Addresses[0].AllocationId' --output text 2>/dev/null || true)
if [[ -z "$ALLOC_ID" || "$ALLOC_ID" == "None" ]]; then
  ALLOC_ID=$(aws ec2 allocate-address --domain vpc --query 'AllocationId' --output text)
  aws ec2 create-tags --resources "$ALLOC_ID" --tags "Key=Name,Value=${NAME}-api-eip" "Key=Project,Value=${PROJECT}"
fi
ASSOC=$(aws ec2 describe-addresses --allocation-ids "$ALLOC_ID" --query 'Addresses[0].InstanceId' --output text)
if [[ "$ASSOC" != "$INSTANCE_ID" ]]; then
  # Disassociate if attached elsewhere
  EXISTING_ASSOC=$(aws ec2 describe-addresses --allocation-ids "$ALLOC_ID" --query 'Addresses[0].AssociationId' --output text)
  if [[ -n "$EXISTING_ASSOC" && "$EXISTING_ASSOC" != "None" ]]; then
    aws ec2 disassociate-address --association-id "$EXISTING_ASSOC"
  fi
  aws ec2 associate-address --instance-id "$INSTANCE_ID" --allocation-id "$ALLOC_ID" >/dev/null
fi
EIP=$(aws ec2 describe-addresses --allocation-ids "$ALLOC_ID" --query 'Addresses[0].PublicIp' --output text)
echo "EC2 Elastic IP: $EIP"

# --- DB subnet group ---
DB_SUBNET_GROUP="${NAME}-db-subnets"
if ! aws rds describe-db-subnet-groups --db-subnet-group-name "$DB_SUBNET_GROUP" &>/dev/null; then
  aws rds create-db-subnet-group \
    --db-subnet-group-name "$DB_SUBNET_GROUP" \
    --db-subnet-group-description "${NAME} RDS subnets" \
    --subnet-ids "$SUBNET_A" "$SUBNET_B" \
    --tags "Key=Project,Value=${PROJECT}" >/dev/null
fi

# --- RDS ---
DB_ID="${NAME}-postgres"
DB_STATUS=$(aws rds describe-db-instances --db-instance-identifier "$DB_ID" \
  --query 'DBInstances[0].DBInstanceStatus' --output text 2>/dev/null || echo "missing")

if [[ "$DB_STATUS" == "missing" ]]; then
  echo "==> Creating RDS Postgres (takes ~5–10 min)..."
  RDS_ARGS=(
    --db-instance-identifier "$DB_ID"
    --db-instance-class "${RDS_INSTANCE_CLASS}"
    --engine postgres
    --master-username "$RDS_USERNAME"
    --master-user-password "$RDS_PASSWORD"
    --allocated-storage "${RDS_ALLOCATED_STORAGE}"
    --storage-type gp3
    --db-name "$RDS_DB_NAME"
    --vpc-security-group-ids "$RDS_SG"
    --db-subnet-group-name "$DB_SUBNET_GROUP"
    --backup-retention-period 7
    --no-publicly-accessible
    --storage-encrypted
    # This instance holds the orders and the ledger — the records of who paid whom.
    # Deletion protection makes destroying it a deliberate two-step rather than one
    # mistyped command, and has to be turned off explicitly before any teardown.
    --deletion-protection
    # Without this the backups are never exercised, and an untested restore is a
    # hope rather than a plan.
    --copy-tags-to-snapshot
    --tags "Key=Project,Value=${PROJECT}"
  )
  # AWS retires Postgres minor versions, so a version pinned in config months ago
  # eventually stops existing and CreateDBInstance fails on a combination error that
  # does not say why. Unset means "newest 16.x available today", which is the major
  # the schema and the test suite are built against.
  RDS_VERSION="${RDS_ENGINE_VERSION:-}"
  if [[ -z "$RDS_VERSION" ]]; then
    RDS_VERSION=$(aws rds describe-db-engine-versions --engine postgres \
      --query 'DBEngineVersions[?starts_with(EngineVersion,`16.`)].EngineVersion' \
      --output text | tr '\t' '\n' | sort -V | tail -1)
    echo "==> Postgres version not pinned; using newest 16.x: ${RDS_VERSION}"
  fi
  if [[ -n "$RDS_VERSION" ]]; then
    RDS_ARGS+=(--engine-version "$RDS_VERSION")
  fi
  aws rds create-db-instance "${RDS_ARGS[@]}" >/dev/null
fi

echo "Waiting for RDS available..."
aws rds wait db-instance-available --db-instance-identifier "$DB_ID"
RDS_HOST=$(aws rds describe-db-instances --db-instance-identifier "$DB_ID" \
  --query 'DBInstances[0].Endpoint.Address' --output text)
echo "RDS: $RDS_HOST"

# URL-encode password for DATABASE_URL (python one-liner; fallback if missing)
if command -v python3 >/dev/null; then
  RDS_PASSWORD_ENC=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=''))" "$RDS_PASSWORD")
else
  RDS_PASSWORD_ENC="$RDS_PASSWORD"
fi
DATABASE_URL_OUT="postgresql://${RDS_USERNAME}:${RDS_PASSWORD_ENC}@${RDS_HOST}:5432/${RDS_DB_NAME}"

# --- Cache subnet group ---
CACHE_SUBNET_GROUP="${NAME}-redis-subnets"
if ! aws elasticache describe-cache-subnet-groups --cache-subnet-group-name "$CACHE_SUBNET_GROUP" &>/dev/null; then
  aws elasticache create-cache-subnet-group \
    --cache-subnet-group-name "$CACHE_SUBNET_GROUP" \
    --cache-subnet-group-description "${NAME} Redis subnets" \
    --subnet-ids "$SUBNET_A" "$SUBNET_B" >/dev/null
fi

# --- ElastiCache Redis ---
CACHE_ID="${NAME}-redis"
CACHE_STATUS=$(aws elasticache describe-cache-clusters --cache-cluster-id "$CACHE_ID" \
  --query 'CacheClusters[0].CacheClusterStatus' --output text 2>/dev/null || echo "missing")

if [[ "$CACHE_STATUS" == "missing" ]]; then
  echo "==> Creating ElastiCache Redis (takes ~5–10 min)..."
  CACHE_ARGS=(
    --cache-cluster-id "$CACHE_ID"
    --cache-node-type "${CACHE_NODE_TYPE}"
    --engine redis
    --num-cache-nodes 1
    --cache-subnet-group-name "$CACHE_SUBNET_GROUP"
    --security-group-ids "$REDIS_SG"
    --tags "Key=Project,Value=${PROJECT}"
  )
  if [[ -n "${CACHE_ENGINE_VERSION:-}" ]]; then
    CACHE_ARGS+=(--engine-version "$CACHE_ENGINE_VERSION")
  fi
  aws elasticache create-cache-cluster "${CACHE_ARGS[@]}" >/dev/null
fi

echo "Waiting for ElastiCache available..."
aws elasticache wait cache-cluster-available --cache-cluster-id "$CACHE_ID"
REDIS_HOST=$(aws elasticache describe-cache-clusters --cache-cluster-id "$CACHE_ID" --show-cache-node-info \
  --query 'CacheClusters[0].CacheNodes[0].Endpoint.Address' --output text)
echo "Redis: $REDIS_HOST"
REDIS_URL_OUT="redis://${REDIS_HOST}:6379/0"

# --- Persist into config.env ---
upsert_config() {
  local key="$1" val="$2"
  if grep -q "^${key}=" "$CONFIG"; then
    # macOS/BSD sed
    if sed --version >/dev/null 2>&1; then
      sed -i "s|^${key}=.*|${key}=${val}|" "$CONFIG"
    else
      sed -i '' "s|^${key}=.*|${key}=${val}|" "$CONFIG"
    fi
  else
    printf '\n%s=%s\n' "$key" "$val" >> "$CONFIG"
  fi
}

upsert_config EC2_HOST "$EIP"
upsert_config DATABASE_URL "$DATABASE_URL_OUT"
upsert_config REDIS_URL "$REDIS_URL_OUT"
upsert_config AWS_REGION "$AWS_REGION"

OUT="${ROOT}/deploy/infra-output.env"
cat > "$OUT" <<EOF
# Generated by create-infra.sh — $(date -u +%Y-%m-%dT%H:%M:%SZ)
AWS_REGION=$AWS_REGION
EC2_INSTANCE_ID=$INSTANCE_ID
EC2_HOST=$EIP
EC2_SG=$EC2_SG
RDS_HOST=$RDS_HOST
DATABASE_URL=$DATABASE_URL_OUT
REDIS_HOST=$REDIS_HOST
REDIS_URL=$REDIS_URL_OUT
EOF

echo
echo "=============================================="
echo " Infra ready"
echo "=============================================="
echo " EC2 SSH:   ssh -i \$DEPLOY_SSH_KEY ec2-user@${EIP}"
echo " RDS:       ${RDS_HOST}"
echo " Redis:     ${REDIS_HOST}"
echo
echo " Next:"
echo "  1. Point DNS A record for your DOMAIN → ${EIP} (Route 53 or registrar)"
echo "  2. SSH in and run:  ./deploy/bootstrap-ec2.sh   (copy repo or use deploy.sh)"
echo "  3. From laptop:     ./deploy/deploy.sh"
echo "  4. On EC2 after DNS: ./deploy/setup-ssl.sh"
echo
echo " Wrote ${OUT} and updated deploy/config.env (EC2_HOST, DATABASE_URL, REDIS_URL)"
echo "=============================================="
