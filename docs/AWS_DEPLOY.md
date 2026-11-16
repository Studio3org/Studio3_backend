# Deploying the backend to AWS

A start-to-finish walkthrough. Every command is meant to be copied as written, from the
repository root (`Backend/Server`) unless a step says otherwise.

Budget about **two hours**, most of it waiting for RDS.

The end state is one EC2 instance running three processes — the API, a Celery worker, and a
Celery beat scheduler — with Postgres on RDS and Redis on ElastiCache.

---

## Where production stands (16 Nov 2026)

Read this first if you are coming back to the server rather than building it.

| | |
|---|---|
| Deployed | `main` at `b0882ce`, on 16 Nov 2026 |
| Database | Migrated to `061_auction_auto_checkout` |
| Stripe | **Live** keys (`sk_live_`) |
| Convelio | **Sandbox** key (`sk_test_`) while the team tests |
| Convelio webhooks | Sandbox webhooks registered to `https://api.studio-3.co/api/webhooks/convelio` |

**Live Stripe with sandbox Convelio means real money but no courier.** It is fine for team
testing and not for real buyers:

- Buying a piece or tickets only charges when someone finishes checkout.
- **Auctions charge on their own.** When an auction closes, the winner's saved card is charged
  off-session for the hammer price. With Convelio on, shipping, insurance and tax follow on
  the same card automatically. To test auctions, bid on $1 pieces, or cancel the auction
  before it closes.
- Shipping paid with a sandbox key is real money, but the sandbox never sends a courier.
  Refund any such order from the admin console.
- With Convelio on, listing a piece for sale requires its packed size, weight and the artist's
  pickup address.

Before real buyers use the app, go through [Going live](#going-live).

### Day-to-day deploys

```bash
git checkout main && git pull
git status --short                 # must print nothing: deploy.sh uploads this folder, not a branch
./deploy/deploy.sh
```

`deploy.sh` runs any new migrations as the API starts, then checks liveness and readiness.

**If SSH times out, your home IP has changed.** Home broadband rotates it, and only the IPs on
the security group can SSH in. Add the new one; don't widen the rule to `0.0.0.0/0`:

```bash
MYIP=$(curl -4 -s https://checkip.amazonaws.com)
aws ec2 authorize-security-group-ingress --group-id sg-040550c52cf32a489 \
  --ip-permissions "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=$MYIP/32,Description=home-$(date +%F)}]"
```

Also set `SSH_CIDR=$MYIP/32` in `deploy/config.env`. The security group is `studio3-ec2-sg`
and the instance is `i-0b90d1b994bf222b2` at `23.23.29.249`. Old IPs pile up on the rule; remove
them now and then with `aws ec2 revoke-security-group-ingress`.

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
   - `AmazonRoute53FullAccess` — **only if your DNS is in Route 53.** studio-3.co is at
     Squarespace, so skip this one and skip `setup-domain.sh` with it.
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

**studio-3.co is registered at Squarespace**, so `setup-domain.sh` does not apply — it only
writes a Route 53 record. Add the record by hand instead, once `create-infra.sh` has printed
the Elastic IP:

> Squarespace → Settings → Domains → studio-3.co → DNS Settings → Add record
>
> | Type | Host | Data | TTL |
> |---|---|---|---|
> | `A` | `api` | the Elastic IP | 1 hour |

`setup-ssl.sh` works regardless of who hosts the zone: certbot proves ownership over port 80
(an HTTP-01 challenge served from `/var/www/certbot`), not through DNS.

To confirm it has propagated:

```bash
dig +short api.studio-3.co     # should print the Elastic IP
```

You can also skip DNS entirely for now and deploy against the raw Elastic IP over HTTP, with
`BACKEND_URL=http://<elastic-ip>`. Add the name and the certificate once the app is proven.

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

### The first admin

A new environment has no admin account and no way to create one from the app, so set these
before the first deploy:

```ini
ADMIN_EMAIL=you@studio-3.co
ADMIN_PASSWORD=<at least 8 characters>
```

The deploy creates that account and marks it admin. It only ever **creates**: if the email
already belongs to an account it is promoted and the existing password is left alone, so
re-deploying can never undo a rotation.

Sign in, change the password, then **clear `ADMIN_PASSWORD` from `config.env`**. A live
credential sitting in a deploy config is a copy of the keys to the refund button.

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
# ./deploy/setup-domain.sh  — Route 53 only. Not applicable: the domain is at Squarespace.
./deploy/setup-ssl.sh       # Let's Encrypt via certbot
```

`setup-ssl.sh` needs the domain to already resolve to the Elastic IP, so add the Squarespace
record from step 3 first and check it:

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

And sign in to the console at `https://api.studio-3.co/admin` — or, once the web app points
here, through **Profile Settings → Staff → Admin console**.

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

## 12. Convelio shipping

Convelio quotes shipping at checkout, books the courier once the order is paid, and sends
tracking back to us through webhooks.

**Settings in `deploy/config.env`:**

```ini
CONVELIO_ENABLED=true
CONVELIO_API_KEY='sk_test_...'     # the sandbox key now; sk_live_... at launch
# CONVELIO_BASE_URL               leave unset: the key's prefix picks sandbox or production
SHIPPING_QUOTE_VALID_HOURS=24
SHIPPING_INSURANCE_DEFAULT=true
ORDER_AUTO_COMPLETE_DAYS=7
```

Convelio is only on when `CONVELIO_ENABLED=true` **and** a key is set. With no key, checkout
falls back to the flat shipping rate.

**Register the webhooks, once for each key.** Run this from `Backend/Server`, with the same key
loaded as the server uses. The first command only lists what's missing; `--apply` registers it:

```bash
.venv/bin/python scripts/register_convelio_webhooks.py https://api.studio-3.co
.venv/bin/python scripts/register_convelio_webhooks.py https://api.studio-3.co --apply
```

It should end with "all five events are registered". The five events are
`custom_quote_ready`, `quote_paid`, `order_created`, `shipment_status_changed` and
`document_ready`. Sandbox and live webhooks are registered separately, so run it again after
changing the key.

Check: `curl -X POST https://api.studio-3.co/api/webhooks/convelio -d '{}'` should answer
**401**, because an unsigned call is rejected.

**Limits:** 100,000 Convelio calls a month across all endpoints. After that every call gets a
429, including checkout quotes and bookings.

---

## Going live

**Stripe** is already live since 16 Nov 2026. If it ever has to be set up again:

1. `sk_live_` and `pk_live_` keys.
2. A live webhook endpoint at `https://api.studio-3.co/api/payments/webhook`, with these events:
   `payment_intent.succeeded`, `payment_intent.payment_failed`, `charge.refunded`,
   `charge.dispute.created`, `charge.dispute.closed`, `account.updated`, `transfer.created`,
   `transfer.failed` and `transfer.reversed`. Its signing secret goes in
   `STRIPE_WEBHOOK_SECRET`; a test-mode secret won't verify live events.
3. Connect activated in live mode. **Artists must onboard again**, because test-mode connected
   accounts don't carry over.
4. `PLATFORM_COMMISSION_BPS` decided for real.

**Convelio, still to do before real buyers:**

1. Put the `sk_live_` key in `CONVELIO_API_KEY` in `deploy/config.env`, straight from the
   password manager, and run `./deploy/deploy.sh`. It only works on
   `https://api.convelio.com/v2`, which the code picks from the key.
2. Register the live webhooks: the two commands in step 12, with the live key loaded.
3. **Check how live webhooks are signed.** Convelio says production deliveries are signed with
   "your live secret token". We asked whether that is the `sk_live_` API key or a separate
   token, and are waiting on their answer. If it's the key, nothing changes. If it's separate,
   the server needs one new setting (for example `CONVELIO_WEBHOOK_SECRET`) **before** going
   live, or every live webhook is rejected with 401 and orders never move past paid.
4. Still waiting on Convelio: instant prices and estimates for US routes, quote validity,
   cancellations, coverage, transit times, and dimensions being swapped in their responses.

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
