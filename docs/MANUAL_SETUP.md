# Manual setup

Everything that has to be done by hand, in an account or a dashboard, before Studio 3 can go
live. None of it can be done from the codebase — it all needs credentials, billing details or
portal access that only you have.

Work through it in order. Sections 1–4 have dependencies (you need the AWS keys before you
can set them on Render); 5–8 are independent of each other.

**Every value you collect goes into the Render dashboard**, not into a file in the repo.
`.env.development` on your machine is the local mirror and is gitignored; `.env.example`
documents what each one is for.

> **Test keys until you are actually live.** Stripe test keys (`sk_test_…`) let the whole
> money flow run without real cards. Swap to live keys as the last step before launch, not
> the first.

---

## Table of contents

1. [Stripe](#1-stripe) — payments, payouts, webhooks
2. [AWS](#2-aws) — media storage and email
3. [Render](#3-render) — where the values go
4. [DNS: studio-3.co](#4-dns-studio-3co) — production links
5. [Apple](#5-apple) — push, deep links, TestFlight
6. [Google / Firebase](#6-google--firebase) — push, signing, Play
7. [Sentry](#7-sentry-optional) — optional
8. [Pre-launch checklist](#8-pre-launch-checklist)

---

## 1. Stripe

Studio 3 takes payment from collectors and pays artists through **Stripe Connect Express**.
Without this section nothing can be bought, bid on or paid out.

### 1.1 Create the account

1. Sign up at [dashboard.stripe.com](https://dashboard.stripe.com).
2. Complete **business verification** — legal entity, bank account, tax details. This can take
   a few days, so start it early even if you are not launching yet.
3. Stay in **Test mode** (toggle, top right) for everything below until launch.

### 1.2 Enable Connect

Connect is what lets artists receive money. Without it, payouts have nowhere to go.

1. **Connect → Get started**.
2. Choose **Express** accounts. (Not Standard: Express keeps onboarding inside our flow and
   Stripe handles the artist's identity and tax collection.)
3. **Connect → Settings → Branding** — add the Studio 3 name, icon and brand colour. Artists
   see this during onboarding, and an unbranded page looks like a phishing attempt.

### 1.3 Get the API key

1. **Developers → API keys**.
2. Copy the **Secret key** (`sk_test_…`, later `sk_live_…`).

→ Render: **`STRIPE_SECRET_KEY`**

> Never commit this or put it in the Flutter app. It can move money.

Also copy the **Publishable key** (`pk_test_…`) — that one *is* safe to ship and goes in the
app's `.env` as `STRIPE_PUBLISHABLE_KEY` (see [§6.4](#64-the-apps-env-file)).

### 1.4 Create the webhook endpoints

**This is the step most likely to be missed, and the consequence is severe:** without it,
collectors are charged and their orders are never marked paid. The service looks completely
healthy while taking money and delivering nothing.

Create **two** endpoints.

**Endpoint A — the platform account**

1. **Developers → Webhooks → Add endpoint**.
2. URL: `https://studio3-backend.onrender.com/api/payments/webhook`
   (later `https://api.studio-3.co/api/payments/webhook` if you move the domain)
3. Select these events:
   - `payment_intent.succeeded`
   - `payment_intent.payment_failed`
   - `charge.refunded`
   - `charge.dispute.created`
   - `charge.dispute.closed`
   - `transfer.created`
   - `transfer.failed`
   - `transfer.reversed`
4. Save, then **Reveal** the signing secret (`whsec_…`). Keep it.

**Endpoint B — connected accounts**

Same URL, but tick **"Listen to events on Connected accounts"**. Select:
- `account.updated`

This is how the platform learns an artist has finished onboarding and can be paid. Without
it, artists complete onboarding and the app never notices.

Save and reveal its signing secret too.

**Combine both secrets, comma-separated, no spaces:**

```
whsec_PLATFORM_SECRET,whsec_CONNECT_SECRET
```

→ Render: **`STRIPE_WEBHOOK_SECRET`**

The code tries each secret in turn, which is why both live in one variable.

### 1.5 Connect return URL

After an artist finishes Express onboarding, Stripe sends them back to a URL.

→ Render: **`CONNECT_ONBOARDING_BASE_URL`** = `https://studio-3.co/connect`

The app handles `/connect/return` and `/connect/refresh` as deep links, so the artist lands
back inside the app rather than on a dead web page.

### 1.6 Test it before trusting it

With test keys set and the service deployed:

1. Buy a piece in the app using card `4242 4242 4242 4242`, any future expiry, any CVC.
2. **Developers → Webhooks → your endpoint → Events**: `payment_intent.succeeded` should show
   a `200`.
3. The order should reach **paid** in `/admin/orders`.

If the webhook shows `400`, the signing secret is wrong. If it shows `500`, check the Render
logs — the event was received but something failed handling it.

To test the money flow locally instead:
```bash
stripe listen --forward-to localhost:9000/api/payments/webhook
```
This prints its own `whsec_…` for local use only.

---

## 2. AWS

Two services, one set of credentials: **S3** stores uploaded images, **SES** sends OTP and
password-reset email.

### 2.1 S3 bucket

1. [S3 console](https://s3.console.aws.amazon.com) → **Create bucket**.
2. Name it, e.g. `studio3-media`. Pick a region and use the same one throughout.
3. **Block all public access: ON.** Images are served through CloudFront, not from the bucket
   directly.
4. **Permissions → CORS**, so the app can upload straight to S3:

```json
[
  {
    "AllowedHeaders": ["*"],
    "AllowedMethods": ["PUT", "POST", "GET", "HEAD"],
    "AllowedOrigins": ["*"],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3000
  }
]
```

→ Render: **`S3_BUCKET`** = your bucket name
→ Render: **`AWS_REGION`** = e.g. `us-east-1`

### 2.2 CloudFront

1. [CloudFront console](https://console.aws.amazon.com/cloudfront) → **Create distribution**.
2. Origin: your S3 bucket. Choose **Origin access control (OAC)** and let it update the bucket
   policy — that is what keeps the bucket private while the images stay reachable.
3. Viewer protocol policy: **Redirect HTTP to HTTPS**.
4. Copy the distribution domain (`dxxxxxxxxxxxxx.cloudfront.net`).

→ Render: **`S3_PUBLIC_BASE_URL`** = `https://dxxxxxxxxxxxxx.cloudfront.net`

### 2.3 SES sender

Without this, **nobody can sign up** — the OTP email never arrives.

1. [SES console](https://console.aws.amazon.com/ses) → same region as above.
2. **Verified identities → Create identity**.
   - Verifying a **domain** (`studio-3.co`) is better than a single address — it lets you send
     from any address on it and improves deliverability. You add the DNS records it gives
     you (see [§4](#4-dns-studio-3co)).
   - Verifying one address is quicker for testing.
3. **Request production access.** A new SES account is sandboxed and can only send to
   addresses you have verified — so in the sandbox, real users get nothing. **Account
   dashboard → Request production access.** Approval takes about a day.

→ Render: **`SES_FROM_EMAIL`** = e.g. `noreply@studio-3.co`

### 2.4 IAM credentials

Create a user with only the access these two services need — not an admin key.

1. [IAM console](https://console.aws.amazon.com/iam) → **Users → Create user**.
2. Attach an inline policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::studio3-media/*"
    },
    { "Effect": "Allow", "Action": ["ses:SendEmail", "ses:SendRawEmail"], "Resource": "*" }
  ]
}
```

3. **Security credentials → Create access key** → *Application running outside AWS*.

→ Render: **`AWS_ACCESS_KEY_ID`** and **`AWS_SECRET_ACCESS_KEY`**

---

## 3. Render

The backend runs as **three separate services**, and two of them fail silently when missing.

| Service | Type | What it does | If it is down |
|---|---|---|---|
| `studio3-backend` | web | The API | Everything, visibly |
| `studio3-jobs` | worker | Celery worker | **Auctions never close.** No error anywhere |
| `studio3-beat` | worker | Celery scheduler | Nothing is ever scheduled. Also silent |

### 3.1 Set the variables

**Dashboard → the service → Environment → Add environment variable.**

**Every variable below must be set on all three services.** The worker closes auctions and
captures cards; it needs the same Stripe and database access as the web service.

| Variable | Value | Section |
|---|---|---|
| `DATABASE_URL` | Aiven Postgres connection string | — |
| `REDIS_URL` | Render Key Value connection string | — |
| `SECRET_KEY` | 32+ random bytes (see below) | — |
| `JWT_SECRET` | 32+ random bytes, **different** from above | — |
| `FRONTEND_URL` | `https://studio-3.co` | [§4](#4-dns-studio-3co) |
| `BACKEND_URL` | `https://studio3-backend.onrender.com` | — |
| `STRIPE_SECRET_KEY` | `sk_live_…` | [§1.3](#13-get-the-api-key) |
| `STRIPE_WEBHOOK_SECRET` | `whsec_A,whsec_B` | [§1.4](#14-create-the-webhook-endpoints) |
| `CONNECT_ONBOARDING_BASE_URL` | `https://studio-3.co/connect` | [§1.5](#15-connect-return-url) |
| `AWS_ACCESS_KEY_ID` | from IAM | [§2.4](#24-iam-credentials) |
| `AWS_SECRET_ACCESS_KEY` | from IAM | [§2.4](#24-iam-credentials) |
| `AWS_REGION` | e.g. `us-east-1` | [§2.1](#21-s3-bucket) |
| `S3_BUCKET` | bucket name | [§2.1](#21-s3-bucket) |
| `S3_PUBLIC_BASE_URL` | CloudFront URL | [§2.2](#22-cloudfront) |
| `SES_FROM_EMAIL` | verified sender | [§2.3](#23-ses-sender) |
| `FIREBASE_SERVICE_ACCOUNT_JSON` | the whole JSON, one line | [§6.1](#61-firebase-project) |
| `IOS_TEAM_ID` | `5XVXDNBNKN` | [§5.1](#51-team-id) |
| `ANDROID_CERT_FINGERPRINTS` | SHA-256, comma-separated | [§6.3](#63-release-keystore) |
| `APP_STORE_URL` | listing URL, once it exists | [§5.5](#55-app-store-listing) |
| `PLAY_STORE_URL` | listing URL, once it exists | [§6.5](#65-play-store-listing) |

Generate the two secrets:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```
Run it twice. They must differ — one signs admin session cookies, the other signs API tokens,
and reusing a value means compromising one compromises both.

### 3.2 The service refuses to start if something is missing

This is deliberate. On boot it checks its configuration and **exits, naming everything
absent at once**. A service that started without `STRIPE_WEBHOOK_SECRET` would look healthy
while never marking a single order paid — failing loudly is the better outcome.

If a deploy fails, read the first lines of the log:
```
Error: Missing required environment variables for production: STRIPE_SECRET_KEY, S3_BUCKET.
```

### 3.3 Custom domain (optional, later)

Only if you want the API on `api.studio-3.co` rather than `…onrender.com`:

1. **Settings → Custom domains → Add**, enter `api.studio-3.co`.
2. Add the CNAME Render gives you ([§4](#4-dns-studio-3co)).
3. Update `BACKEND_URL`, the Stripe webhook URLs, and the hosts in the app's iOS entitlement
   and Android manifest.

---

## 4. DNS: studio-3.co

You own the domain; these records make it useful. Add them at your registrar.

| Purpose | Type | Name | Value |
|---|---|---|---|
| SES domain verification | CNAME ×3 | given by SES | given by SES |
| SES DKIM | CNAME ×3 | given by SES | given by SES |
| API subdomain *(optional)* | CNAME | `api` | given by Render |
| Deep links *(see below)* | A / CNAME | `@` | where studio-3.co is hosted |

### What `studio-3.co` has to serve for deep links

For a shared link like `https://studio-3.co/piece/abc` to open the app, that domain must
serve two files over HTTPS, **with no redirect**:

- `/.well-known/apple-app-site-association`
- `/.well-known/assetlinks.json`

**The backend already serves both.** So point `studio-3.co` at the Render service and it
works — no separate web app needed.

**Until you do**, links still work through the custom scheme `studio3://`, which needs no
domain verification at all. QR codes and shared links fall back to it. So this is not
blocking; it only changes how links look.

Verify after pointing it:
```bash
curl -sI https://studio-3.co/.well-known/apple-app-site-association | head -3
# expect: HTTP/2 200, content-type: application/json, and NO 301/302
```

---

## 5. Apple

You already have a Developer Program membership (TestFlight proves it), so this is
configuration rather than signup.

### 5.1 Team ID

Already known: **`5XVXDNBNKN`**. It is in `ios/Runner.xcodeproj` as `DEVELOPMENT_TEAM`, and
at [developer.apple.com/account](https://developer.apple.com/account) → Membership details.

→ Render: **`IOS_TEAM_ID`**

### 5.2 App ID capabilities

**[developer.apple.com](https://developer.apple.com/account) → Certificates, IDs & Profiles →
Identifiers → `com.studio3.discover` → Edit.**

Tick both:

- **Push Notifications** — without it iOS receives no push at all
- **Associated Domains** — without it `https://` links never open the app

Save, then **regenerate your provisioning profiles** and re-download them in Xcode.
Capabilities added after a profile was issued are ignored until the profile is reissued, and
nothing warns you.

### 5.3 The push entitlement

Two file edits, both in the Flutter repo. I deliberately left these for you so they land with
your signing work.

**`ios/Runner/Runner.entitlements`** — add `aps-environment` alongside the existing
associated-domains block:

```xml
<key>aps-environment</key>
<string>development</string>
```

Use `development` for TestFlight builds; Xcode substitutes `production` for App Store
builds automatically when the capability is on the App ID.

**`ios/Runner/Info.plist`** — add, inside the top-level `<dict>`:

```xml
<key>UIBackgroundModes</key>
<array>
  <string>remote-notification</string>
</array>
```

### 5.4 APNs key for Firebase

Firebase sends iOS push *through* APNs and needs a key to do it.

1. **Certificates, IDs & Profiles → Keys → +**.
2. Name it, tick **Apple Push Notifications service (APNs)**, continue, register.
3. **Download the `.p8`.** You can only download it once — losing it means making a new key.
4. Note the **Key ID** and your Team ID.
5. In Firebase: **Project settings → Cloud Messaging → Apple app configuration → APNs
   Authentication Key → Upload**. Provide the `.p8`, Key ID and Team ID.

Without this, iOS devices register for push and simply never receive any.

### 5.5 App Store listing

When you are ready to leave TestFlight: App Store Connect → new app → the usual metadata,
screenshots, privacy details, review.

→ Render: **`APP_STORE_URL`** once the listing exists (used on the share page for people who
do not have the app).

---

## 6. Google / Firebase

### 6.1 Firebase project

1. [console.firebase.google.com](https://console.firebase.google.com) → Add project.
2. **Project settings → Service accounts → Generate new private key.** Downloads a JSON file.
3. The backend needs its **entire contents on one line**:

```bash
python3 -c "import json,sys; print(json.dumps(json.load(open('service-account.json'))))" | pbcopy
```

→ Render: **`FIREBASE_SERVICE_ACCOUNT_JSON`**

Without it the server sends no push at all — it logs "Firebase not configured" and carries
on.

### 6.2 App config files

**Android** — `android/app/google-services.json` is already in the repo. ✅

**iOS** — `ios/Runner/GoogleService-Info.plist` is **missing**. Until it is added, iOS gets
no push even with everything in [§5](#5-apple) done.

1. Firebase console → **Project settings → Your apps → Add app → iOS**.
2. Bundle ID: `com.studio3.discover`.
3. Download `GoogleService-Info.plist`.
4. Add it to `ios/Runner/` **in Xcode** (drag into the Runner group, tick "Copy items if
   needed" and the Runner target). Copying it in Finder alone does not add it to the build.

### 6.3 Release keystore

**Your release builds are currently signed with the debug key**
(`android/app/build.gradle.kts` uses `signingConfigs.getByName("debug")` for release). Play
will reject that, and the key is per-machine — build on another laptop and deep links stop
verifying.

```bash
keytool -genkey -v -keystore ~/studio3-release.jks \
  -keyalg RSA -keysize 2048 -validity 10000 -alias studio3
```

**Back up that `.jks` and its passwords somewhere permanent.** Lose it and you can never
update an existing Play listing — there is no recovery.

Then wire it up. Create `android/key.properties` (gitignored):

```properties
storePassword=…
keyPassword=…
keyAlias=studio3
storeFile=/Users/you/studio3-release.jks
```

and point the release `signingConfig` at it instead of `debug`.

Read the fingerprint:
```bash
keytool -list -v -keystore ~/studio3-release.jks -alias studio3 | grep SHA256
```

→ Render: **`ANDROID_CERT_FINGERPRINTS`**

List **every** key whose builds should verify, comma-separated:
- the release key above
- your debug key (so internally-shared APKs keep working):
  `keytool -list -v -keystore ~/.android/debug.keystore -alias androiddebugkey -storepass android | grep SHA256`
- once on Play with Play App Signing, **Google's** key: Play Console → your app → **Setup →
  App signing → SHA-256 certificate fingerprint**

Listing only one means links verify for some of your own builds and silently not others.

### 6.4 The app's `.env` file

The Flutter app reads three values from `app/studio3/.env` (gitignored):

```
NEXT_PUBLIC_API_URL=https://studio3-backend.onrender.com
NEXT_PUBLIC_APP_URL=https://studio-3.co
STRIPE_PUBLISHABLE_KEY=pk_live_…
```

The publishable key is safe to ship — it is what lets the Stripe SDK take card details
directly, so card data never touches our server. **Never put the secret key here.**

### 6.5 Play Store listing

Play Console → create app → store listing, content rating, data safety, release.

→ Render: **`PLAY_STORE_URL`** once it exists.

---

## 7. Sentry (optional)

Error tracking. Unset means Sentry is never initialised — no cost, no noise.

1. [sentry.io](https://sentry.io) → new project → Python/Flask.
2. Copy the DSN.

→ Render: **`SENTRY_DSN`**

Payloads are scrubbed before sending — webhook bodies, shipping addresses and client secrets
are stripped, so a crash report cannot become a copy of a customer's details.

---

## 8. Pre-launch checklist

Work down it. Each line is something that silently does not work if skipped.

### Money
- [ ] Stripe business verification complete
- [ ] Connect enabled, Express, branding set
- [ ] `STRIPE_SECRET_KEY` live key on all three Render services
- [ ] Both webhook endpoints created, **both** secrets in `STRIPE_WEBHOOK_SECRET`
- [ ] A real test purchase reached **paid** in `/admin/orders`
- [ ] An artist completed Connect onboarding and shows as payout-enabled

### Accounts and email
- [ ] SES out of the sandbox (production access approved)
- [ ] Sender verified; a real signup received its OTP

### Media
- [ ] S3 bucket created, CORS set, public access blocked
- [ ] CloudFront serving, `S3_PUBLIC_BASE_URL` set
- [ ] An image uploaded from the app and displayed back

### Services
- [ ] All three Render services deployed and green
- [ ] **Worker running** — an auction closed on its own
- [ ] **Beat running** — exactly one instance
- [ ] `/health` returns `ok`

### Push
- [ ] Firebase service account JSON on Render
- [ ] `GoogleService-Info.plist` added to the iOS project in Xcode
- [ ] APNs `.p8` uploaded to Firebase
- [ ] `aps-environment` and `UIBackgroundModes` added
- [ ] Push Notifications capability on the App ID, profiles regenerated
- [ ] A real push received on a real iPhone **and** a real Android device

### Links
- [ ] `IOS_TEAM_ID` and `ANDROID_CERT_FINGERPRINTS` on Render
- [ ] `curl` of both `.well-known` files returns 200 JSON with no redirect
- [ ] Associated Domains capability on the App ID
- [ ] A shared link opened the app on both platforms
- [ ] A printed QR code scanned and opened the right piece

### Android
- [ ] Release keystore created and **backed up**
- [ ] `build.gradle.kts` signs release with it, not debug
- [ ] Play App Signing fingerprint added to `ANDROID_CERT_FINGERPRINTS`

### Before the first real event
- [ ] A full auction run end to end on staging: bid → close → capture → checkout → payout
- [ ] A declined card tested — confirm the cascade passes the piece on
- [ ] QR sheet printed and scanned **on paper**, at the distance people will actually stand

---

## Still open, and not mine to decide

These have been outstanding since the start and are product decisions rather than setup:

- **Currency** — everything assumes USD (`PLATFORM_CURRENCY`)
- **Bid increment table** — live in production code, never signed off:
  under $100 → $5 · $100–499 → $10 · $500–1,999 → $25 · $2,000–9,999 → $100 · $10,000+ → $500
- **The 15% artist list** — a reduced commission tier was mentioned; nobody has supplied the list
- **Shipping revenue** — currently booked to platform revenue, not the seller

The increment table is the one worth settling first: it is already governing real bids.
