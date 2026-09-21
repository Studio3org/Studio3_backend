# Runbook

What this service is made of, what runs on a schedule, and what to check when something is
wrong. Written for whoever is on the end of the problem — including you, in six months,
having forgotten all of it.

For the API surface see [API.md](API.md). For what each environment variable does and where
its value comes from, see `.env.example`.

---

## The three processes

Nothing works if any of these is missing, and two of them are easy to forget because nothing
errors when they are down — things simply stop happening.

| Process | Command | On AWS | What breaks without it |
|---|---|---|---|
| **web** | `gunicorn` (see `render.yaml`) | `studio3-api.service` | Everything, visibly. |
| **worker** | `celery -A src.jobs.celery_app.celery_app worker` | `studio3-worker.service` | **Auctions never close.** Silent. |
| **beat** | `celery -A src.jobs.celery_app.celery_app beat` | `studio3-beat.service` | Nothing is ever scheduled. Also silent. |

Render runs them as three services (`render.yaml`); AWS runs them as three systemd units on
one EC2 box (`deploy/systemd/`). Same three processes either way.

**Beat must be a single instance.** Two beats means every schedule fires twice — two closes
for one auction, two capture attempts on one card.

On macOS the worker needs `--pool=solo`; Celery's prefork pool fails there on Python 3.13+.
Linux uses the default pool, which is what `render.yaml` runs.

---

## Scheduled jobs

Every one re-reads and re-validates its target under a row lock, so redelivery is wasteful
but never wrong.

| Job | Every | What it does | If it stops |
|---|---|---|---|
| `auctions.close_expired` | 1 min | Captures the winner, releases everyone else | Auctions run past their end. Money stays held, nobody is charged, nobody is told. **The worst one to lose.** |
| `auctions.expire_winner_windows` | 1 min | Passes a piece on when a declined winner's window runs out | A stuck winner blocks the piece indefinitely |
| `auctions.refresh_holds` | 03:00 | Re-authorises holds before the card lets go | Long auctions close on dead authorisations and capture nothing |
| `events.expire_waitlist_offers` | 5 min | Placeholder — deferred with ticketing | — |
| `events.archive_past` | hourly | Placeholder — deferred | — |
| `ledger.reconcile` | 04:00 | Compares the books against Stripe, reports drift | Drift accumulates unnoticed |

---

## When something is wrong

### An auction did not close

1. Is the **worker** running? This is the answer most of the time.
2. `/admin/auctions` → "Running now". Is `closes_at` actually in the past?
3. A bid inside the last five minutes extends a *standalone* auction, repeatedly and by
   design. An **event** auction never extends — if one looks extended, something is wrong.

### A winner was charged nothing / the piece is stuck

`/admin/auctions` → "Needs attention".

- **`awaiting_payment`** — their card was declined at close. A clock is running; when it
  expires the piece passes to the next bidder. Both parties were notified. The winner can
  fix it themselves from the app.
- **`needs_seller_action`** — the auction ended without a sale (reserve not met, or every
  bidder's card failed). Nothing was charged to anyone. It waits on the seller.

`cascade_depth` above zero means this has already failed for at least one bidder.

### An artist has not been paid

`/admin/disputes` → failed payouts. Two states, and they mean opposite things:

- **`transfer_failed`** — Stripe refused. Retryable, and the retry button is safe.
- **`blocked`** — deliberately held by a chargeback or a refund. **Do not retry.** Money is
  being clawed back; releasing it pays an artist out of the platform's own pocket.

### The books do not match Stripe

`/admin/audit`, filtered to `ledger_drift_detected`. The nightly job records a finding when
the clearing account and Stripe's balance differ by more than $1.

**It never corrects anything, and neither should you without finding the cause first.** An
adjustment made before the cause is known hides the bug and destroys the evidence. Usual
suspects: a refund issued by hand in the Stripe dashboard (no webhook), a webhook that
failed every retry, a fee that was estimated rather than read.

### Somebody changed something and nobody knows who

`/admin/audit`. Refunds, payout releases and retries, dispute resolutions, cancellations.
`system` in the Who column means a scheduled job acted on its own — there is nobody to ask.

The trail can have holes by design: an audit write that failed is swallowed rather than
allowed to fail the action it was describing. Letting bookkeeping veto a refund would make
the operator retry, and a retried refund is a second refund.

---

## Deploying

**Render (staging):** push. `render.yaml` runs `alembic upgrade head` in the start command.

**AWS (production):** `./deploy/deploy.sh` from a laptop with `deploy/config.env` filled in.
It rsyncs the code, writes `/opt/studio3/.env.production`, then restarts api, worker and beat
in that order — the API's `ExecStartPre` owns the migration, so the job processes never start
against a schema it has not reached.

First time on a fresh account, in order:

```
./deploy/create-infra.sh    # VPC bits, RDS, ElastiCache, EC2 + Elastic IP
./deploy/setup-domain.sh    # Route 53 A record -> the Elastic IP
ssh ... 'bash bootstrap-ec2.sh'
./deploy/deploy.sh
ssh ... 'cd /opt/studio3 && bash deploy/setup-ssl.sh'
```

Either way `alembic upgrade head` runs at start, so **a broken migration is an outage**. CI
applies the full chain to an empty database on every push; that step is the one worth
watching.

Logs on AWS: `journalctl -u studio3-worker -f`, or `/opt/studio3/logs/`.

Config is checked at boot and the process **refuses to start** with anything required
missing, naming everything absent at once. That is deliberate: a service that starts without
`STRIPE_WEBHOOK_SECRET` looks healthy while never marking a single order paid.

---

## Things that are meant to be like this

Worth knowing before you "fix" one of them:

- **Redis fails open.** A Redis outage degrades; it does not stop the service. `/health` says
  `degraded` while still serving.
- **`GET /`** is liveness and touches nothing external — Render and systemd restart on it, and
  a database blip must not be read as a dead process. Point uptime monitoring at `/health` instead.
- **No Stripe key = dev mode.** Checkout auto-confirms and holds are granted without a card.
  Intentional for local work; catastrophic if it ever happened in production, which is why
  the key is required at boot there.
- **Being outbid does not release your hold.** You are still in line if the bid above you
  falls through. Only raising your own bid moves your money.
- **A losing bidder's hold is released only once someone's money is genuinely captured.** That
  is what makes the winner cascade possible.
- **Commission is snapshotted on the order.** Changing the platform rate never restates a past
  sale.
