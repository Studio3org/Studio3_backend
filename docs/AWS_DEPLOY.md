# Deploying the backend to AWS

A start-to-finish walkthrough. Every command is meant to be copied as written, from the
repository root (`Backend/Server`) unless a step says otherwise.

Budget about **two hours**, most of it waiting for RDS.

The end state is one EC2 instance running three processes — the API, a Celery worker, and a
Celery beat scheduler — with Postgres on RDS and Redis on ElastiCache.

---

## Before you start

You need an AWS account with billing enabled, and the repository checked out.

Check what you already have:

```bash
aws --version                  # want v2.x
aws sts get-caller-identity    # want your account id, not an error
ls ~/.ssh/studio3-key.pem      # want the file to exist
curl -4 ifconfig.me            # your IPv4 address — note it down
```

Use `curl -4`. Plain `curl ifconfig.me` may answer with an IPv6 address, and a security group
rule for SSH needs IPv4.

---

## 1. An IAM user for deploying

Do **not** reuse the S3 keys in `studio3-s3-dev_accessKeys.csv`. Those are deliberately
least-privilege and cannot create servers.

1. AWS Console → **IAM** → **Users** → **Create user**
2. Name: `studio3-deploy`. Do not give it console access.
3. **Set permissions** → *Attach policies directly*, and tick:
   - `AmazonEC2FullAccess`
   - `AmazonRDSFullAccess`
   - `AmazonElastiCacheFullAccess`
   - `IAMReadOnlyAccess`
   - `AmazonRoute53FullAccess` — only if your DNS is in Route 53
4. Create the user, open it, → **Security credentials** → **Create access key**
5. Use case: **Command Line Interface (CLI)**, acknowledge the warning, create
6. Copy both values. **The secret is shown once.**

Then:

```bash
aws configure
#   AWS Access Key ID:     <paste>
#   AWS Secret Access Key: <paste>
#   Default region name:   us-east-1
#   Default output format: json
```

Confirm it worked — this must print your account, not an error:

```bash
aws sts get-caller-identity
```

> These are broad permissions, appropriate for a one-person deploy. If other people get
> access later, narrow them.

---

## 2. The SSH key pair

One command creates the key at AWS and saves the private half locally:

```bash
aws ec2 create-key-pair --key-name studio3-key --region us-east-1 \
  --query 'KeyMaterial' --output text > ~/.ssh/studio3-key.pem
chmod 600 ~/.ssh/studio3-key.pem
```

Check it looks right:

```bash
head -1 ~/.ssh/studio3-key.pem    # -----BEGIN RSA PRIVATE KEY-----
```

**AWS shows the private key exactly once.** Lose this file and you cannot SSH to the server —
the only recovery is deleting the key pair and rebuilding the instance. Back it up.

If the name is already taken, either use the existing `.pem` you have, or pick another name
and set `EC2_KEY_NAME` to match in step 4.

---

## 3. Decide about the domain

Optional at this stage. You can deploy against the raw IP address and add the domain later.

Check whether your DNS is already in Route 53:

```bash
aws route53 list-hosted-zones --query 'HostedZones[].Name' --output text
```

- **`studio-3.co.` appears** → `setup-domain.sh` will work. Note the zone name for step 4.
- **Nothing, or a different registrar** → skip `setup-domain.sh`. After the instance exists,
  add an `A` record at your current DNS provider pointing `api.studio-3.co` at the Elastic IP
  that `create-infra.sh` prints. `setup-ssl.sh` works either way once the name resolves.

---

## 4. Fill in the infrastructure settings

```bash
cp deploy/config.env.example deploy/config.env
```

Open `deploy/config.env` and set **only these seven** for now. The rest are for step 7.

```ini
AWS_REGION=us-east-1
PROJECT=studio3
EC2_KEY_NAME=studio3-key
SSH_CIDR=<your IPv4 from step 0>/32     # e.g. 203.0.113.9/32
RDS_DB_NAME=studio3
RDS_USERNAME=studio3
RDS_PASSWORD=<generate one, see below>
```

Generate the database password and keep it:

```bash
openssl rand -base64 24 | tr -d '/+=' | head -c 24; echo
```

> `SSH_CIDR` is your current IP. Home broadband usually rotates it, and when it changes you
> are locked out of SSH until you update the security group. Never widen it to `0.0.0.0/0` —
> that exposes the server to every SSH scanner on the internet.

`deploy/config.env` is git-ignored and will hold every production secret. It never gets
committed and never leaves your machine.

---

## 5. Create the infrastructure

```bash
./deploy/create-infra.sh
```

Roughly fifteen minutes, nearly all of it RDS. It creates security groups, an RDS Postgres 16
instance, an ElastiCache Redis node, a `t3.small` EC2 instance, and an Elastic IP.

It is safe to re-run: it finds what already exists instead of duplicating it.

When it finishes it prints the endpoints. **Copy them into `deploy/config.env`:**

```ini
EC2_HOST=<the Elastic IP>
DATABASE_URL=postgresql://studio3:<RDS_PASSWORD>@<rds endpoint>:5432/studio3
REDIS_URL=redis://<elasticache endpoint>:6379/0
```

---

## 6. Prepare the server

```bash
./deploy/bootstrap-ec2.sh
```

Installs Python, nginx and system packages. A couple of minutes.

If it cannot connect, the usual cause is `SSH_CIDR` — check your IP has not changed:

```bash
curl -4 ifconfig.me
```

---

## 7. Fill in the application settings

Back in `deploy/config.env`. **`deploy.sh` refuses to run until every one of these is set**,
because the service will not boot without them:

```ini
FRONTEND_URL=https://studio3-eta.vercel.app
BACKEND_URL=https://api.studio-3.co        # or http://<Elastic IP> for now
CORS_ORIGINS=
PLATFORM_COMMISSION_BPS=1000               # 1000 = 10%. Staging runs this. 2000 = 20%.
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...,whsec_...  # both, comma-separated
CONNECT_ONBOARDING_BASE_URL=https://api.studio-3.co/connect
AWS_ACCESS_KEY_ID=...                      # the S3 keys — these ARE the right ones here
AWS_SECRET_ACCESS_KEY=...
S3_BUCKET=studio3-...
S3_PUBLIC_BASE_URL=https://....cloudfront.net
```

**Leave `JWT_SECRET` and `SECRET_KEY` empty.** The first deploy generates them and writes them
back into this file. Do not clear them afterwards: changing `JWT_SECRET` signs out every user,
and changing `SECRET_KEY` also ends every admin session.

`PLATFORM_COMMISSION_BPS` has no default anywhere, deliberately — 10% and 20% differ by half
of what an artist earns, and that is not a number to inherit by accident.

Copy the Stripe values from your current Render environment so staging and production agree.

---

## 8. Deploy

```bash
./deploy/deploy.sh
```

It syncs the code, writes `.env.production` on the server, installs dependencies, runs
`alembic upgrade head`, and starts all three services plus the beat watchdog.

Migrations run from the API service's start-up, before the worker and beat come up, so no job
process ever sees a half-migrated schema.

---

## 9. Domain and HTTPS

```bash
./deploy/setup-domain.sh    # Route 53 only; skip if your DNS is elsewhere
./deploy/setup-ssl.sh       # Let's Encrypt via certbot
```

`setup-ssl.sh` needs the domain to already resolve to the Elastic IP. If you added the record
manually, wait for it to propagate first:

```bash
dig +short api.studio-3.co
```

---

## 10. Re-point Stripe

**Easy to forget, and it breaks payments silently.**

Both webhook destinations currently point at `studio3-backend.onrender.com`. In the Stripe
dashboard → Webhooks, update both to the new host:

```
https://api.studio-3.co/api/payments/webhook
```

A new endpoint gets a **new signing secret**. Copy both, rebuild the comma-separated value in
`deploy/config.env`, and run `./deploy/deploy.sh` again.

Skip this and payments still succeed at Stripe while orders never reach `paid` — no error
anywhere, because the signature check simply rejects every delivery.

---

## 11. Verify

```bash
curl https://api.studio-3.co/            # liveness — touches nothing external
curl https://api.studio-3.co/health      # readiness — checks Postgres and Redis
```

Then on the server:

```bash
ssh -i ~/.ssh/studio3-key.pem ec2-user@<EC2_HOST>

systemctl status studio3-api studio3-worker studio3-beat
systemctl list-timers studio3-beat-watchdog.timer
journalctl -u studio3-api -n 50 --no-pager
```

**If `studio3-beat` is not running, no auction will ever close** and no abandoned checkout will
ever release its artwork. It is the one service whose absence is invisible from the outside.

Finally, the only test that really counts: point the app at the new backend and complete one
purchase end to end — checkout, webhook, buyer confirmation, payout.

---

## Going live

Everything above is still Stripe **test** mode. Switching means:

1. `sk_live_` and `pk_live_` keys
2. New webhook endpoints in live mode, with new signing secrets
3. **Artists must onboard again** — test-mode connected accounts do not carry over
4. Deciding `PLATFORM_COMMISSION_BPS` for real

---

## When something goes wrong

`docs/RUNBOOK.md` covers the operational failures: an auction that did not close, an artist who
has not been paid, a piece stuck on reserved, books that disagree with Stripe.

## Tearing it down

RDS has deletion protection, so it must be disabled before the instance can be deleted. That is
deliberate — it holds the orders and the ledger.

```bash
aws rds modify-db-instance --db-instance-identifier studio3-db \
  --no-deletion-protection --apply-immediately
```
