# Studiothree Discover — Current System Features & Complete Flows

**Document type:** Living product + engineering snapshot of what exists **today** in the backend  
**Product:** Studiothree Discover (mobile + web)  
**Codebase:** `Backend/Server` (Flask API)  
**Companion docs:**
- Endpoint contract & request/response shapes → [`docs/API.md`](../docs/API.md)
- Setup / run / env → [`docs/README.md`](../docs/README.md)
- Planned marketplace payment/escrow (not built yet) → [`Art-Marketplace-Phase1-PRD.md`](./Art-Marketplace-Phase1-PRD.md)

**Status legend used below**
| Tag | Meaning |
|---|---|
| **Live** | Implemented, blueprint registered, usable end-to-end |
| **Stub** | Endpoint exists but behavior is placeholder / incomplete |
| **Schema-ready** | Models/migrations (and sometimes unused code) exist; HTTP not wired or incomplete |
| **Deferred** | Explicitly out of current product surface |

---

## 1. Product summary

Studiothree Discover is a social + discovery platform for artists and collectors, with a lightweight marketplace checkout path. Users create accounts, complete onboarding, publish **Pieces** (artwork) and **Scenes** (process/WIP posts), follow others (including private-account follow requests), engage (like / save / comment), browse feeds, DM each other, organize saves into collections, group pieces into series, and optionally sell pieces via a collect → confirm → ship order lifecycle.

**What the product is today:** Instagram-style social graph + content + chat, plus a **dev-mode checkout** that marks orders paid without a real payment provider.

**What it is not yet:** Stripe payments, Stripe Connect artist payouts, escrow/release, carrier shipping, personalized For You, piece-scoped inquiries UI, or Google OAuth login.

---

## 2. Stack & architecture

| Layer | Choice |
|---|---|
| API | Flask 3, blueprints, CORS (`FRONTEND_URL`, credentials) |
| ORM / DB | SQLAlchemy 2.0 + PostgreSQL + Alembic (migrations through `021_…`) |
| Cache / sessions / OTP | Redis |
| Auth | JWT access tokens + httpOnly `refreshToken` cookie; Redis session store |
| Media | S3 presigned PUT (boto3); local PUT/GET fallback when S3 unset |
| Email | AWS SES (OTP + password reset); skipped when `SES_FROM_EMAIL` unset |
| Push | Firebase Admin / FCM; fail-open when unconfigured |
| Realtime | Flask-SocketIO + gevent + Redis pub/sub (`studio3-chat`) |
| Prod process | `gunicorn -k gevent -w 1` (required for Socket.IO); Python 3.12 |

**Request layering:** `routes` → `controllers` → `services` / `DAOs` → SQLAlchemy models.

**Registered blueprints** (`src/app.py`):
`/api/auth`, `/api/user`, `/api/users`, `/api/media`, `/api/pieces`, `/api/posts`, `/api` (social), `/api/feed`, `/api/series`, `/api/notifications`, `/api/orders`, `/api/conversations`, `/api/collections`.

**Not registered:** `/api/inquiries` (code kept; messaging via Conversations).

**Response envelope:**
```json
{ "success": true, "message": "...", "data": { ... } }
// or
{ "success": false, "message": "..." }
```

**Auth decorators:**
| Decorator | Behavior |
|---|---|
| *(none / public)* | No token |
| `optional_auth` | Works anonymous; enriches viewer fields when Bearer present |
| `auth_required` | Valid JWT + Redis session |
| `onboarding_required` | Auth + `email_verified` + `onboarding_complete` |

---

## 3. Feature inventory (what exists)

### 3.1 Authentication & account lifecycle — **Live**

| Feature | Status | Notes |
|---|---|---|
| Email OTP generate / resend / peek-verify | Live | Redis OTP, 5 min TTL, rate limits |
| Register (username + password + OTP) | Live | Optional `phone`; sets session + refresh cookie |
| Login (username **or** email + password) | Live | `@` → treat as email |
| Refresh / logout / logout-all | Live | Cookie-based refresh |
| Forget / reset password | Live | Email link + hashed token (1h) |
| Username availability check + suggestions | Live | Blocklist, history, Redis cache |
| Change username (30-day cooldown) | Live | Migrates S3 media prefix; history reserve |
| Change password (authenticated) | Live | Revokes other sessions |
| Change email (OTP to new address) | Live | Two-step request → confirm |
| Google / social OAuth login | Schema-ready | `Account` model + nullable password; **no OAuth routes** |
| Piece-scoped inquiries API | Deferred | Blueprint commented out; use DMs |

### 3.2 Onboarding & identity — **Live**

| Feature | Status | Notes |
|---|---|---|
| Role selection | Live | `artist` \| `collector` \| `enthusiast` — interest only, **not** permissions |
| Taste preferences | Live | ≥3 each of mediums / styles / themes |
| Profile / cover photos (or skip) | Live | Via media presign |
| Mark onboarding complete | Live | Gates content mutations |
| Profile fields | Live | name, bio (≤250), location, phone, pronouns, lat/lng, mediums |
| Magnum-opus banner | Live | Manual pin (`piece`/`post`) or auto rule `most_saved` / `most_recent` / `none` |
| Public vs private profile | Live | Instagram-style |
| Message permission | Live | `everyone` / `following` / `no_one` |
| Notification preferences | Live (partial) | Push gates live; `dailyDigest` stored only — **no scheduler** |

### 3.3 Seller mode — **Live**

| Feature | Status | Notes |
|---|---|---|
| Enable seller (`location` or use profile location) | Live | Required to list for sale |
| Disable seller | Live | Blocked if live for-sale listings or in-progress sales |
| Seller status / analytics | Live | saves, likes, inquiries (legacy table count), sales |

Roles do **not** gate buying or posting: artists can buy; collectors can post.

### 3.4 Media upload — **Live**

| Feature | Status | Notes |
|---|---|---|
| Presign upload | Live | `purpose`: `profile` \| `cover` \| `piece` \| `post` \| `chat` |
| Image types | Live | jpeg / png / webp (max 20MB) |
| Video | Live | mp4 (max 100MB) |
| Local upload fallback | Live | When S3 not configured (`devMode`) |
| URL ownership validation | Live | Piece/post create must use caller’s media prefix |

### 3.5 Pieces (artwork) — **Live**

| Feature | Status | Notes |
|---|---|---|
| Create / edit / soft-delete | Live | Soft-delete via `deleted_at` |
| Draft / live / sold / delisted / reserved | Live | Sale + checkout drive reserved/sold |
| For-sale listing fields | Live | price, dimensions, shipping region, materials, style tags, provenance, framing, handling notes, AI disclosure, alt text, year |
| Detail enrichment | Live | author, likes, comments, saves, series, related posts |
| Related scenes | Live | Posts with `linkedPieceId` |
| Profile Work / For Sale tabs | Live | Privacy-gated for private accounts |
| Shipping quote | Live | Flat rates only (no carriers) |
| Collect (start checkout) | Live | Creates `pending_payment` order |

**Piece status machine:**
```
draft ⇄ live → reserved → sold
              ↘ delisted
cancel order: reserved|sold → live (when applicable)
```

### 3.6 Scenes (API: `posts`) — **Live**

| Feature | Status | Notes |
|---|---|---|
| Create / edit / soft-delete image or video scenes | Live | Not sellable |
| Link to a piece (`linkedPieceId`) | Live | Own piece only |
| `isProcess` flag | Live | Defaults false |
| Profile Scenes tab | Live | Privacy-gated |

UI terminology: **Scene** = API resource **`post`**.

### 3.7 Social graph & engagement — **Live**

| Feature | Status | Notes |
|---|---|---|
| Follow / unfollow (public accounts) | Live | Instant `accepted` |
| Follow requests (private accounts) | Live | `pending` → accept / decline |
| Followers / following lists | Live | Privacy-gated |
| Block / unblock | Live | Severs follows; hides profiles; blocks DM start |
| Like / unlike piece or scene | Live | Idempotent |
| Save / unsave (quick bookmark) | Live | Separate from named collections |
| Comments create / list | Live | Create needs onboarding; list is public |
| Notifications for social actions | Live | Skip self-actions |

### 3.8 Feeds & discovery — **Live / Stub**

| Feature | Status | Notes |
|---|---|---|
| Following feed | Live | Accepted follows + self; cursor pagination |
| Explore feed | Live | Global live content; optional `?medium=` |
| For You feed | **Stub** | Same as explore + `"stub": true` |
| Nearby sellers | Live | Haversine on lat/lng; no PostGIS |

### 3.9 Series — **Live** (no delete series)

| Feature | Status | Notes |
|---|---|---|
| Create series (+ optional pieces) | Live | A piece belongs to at most one series |
| Rename / reorder pieces | Live | |
| Add / remove piece | Live | |
| Public list | Live | Only series with **>1** piece |
| Delete entire series | Missing | No DELETE series endpoint |

### 3.10 Collections (saved folders) — **Live**

| Feature | Status | Notes |
|---|---|---|
| CRUD named collections | Live | Instagram-style folders |
| Add / remove piece or scene items | Live | |
| Cover from most recent item | Live | |

### 3.11 Notifications & push — **Live** (digest deferred)

| Feature | Status | Notes |
|---|---|---|
| In-app activity feed | Live | Excludes DM `message` type from list |
| Mark read / read-all / unread count | Live | |
| Device token register / unregister | Live | ios / android / web |
| FCM push on activity | Live if Firebase configured | Fail-open otherwise |
| Daily digest emails/pushes | Schema-only prefs | No job runner |

**Notification types (activity):** `follow`, `follow_request`, `like`, `save`, `comment`, `purchase`, (`inquiry` legacy). DMs use push-only path, not the activity list.

### 3.12 Conversations (DMs) — **Live**

| Feature | Status | Notes |
|---|---|---|
| 1:1 threads REST | Live | Inbox, requests folder, search users |
| Message requests | Live | `pending` if recipient doesn’t follow sender |
| Accept / decline / mark read | Live | Decline → `closed` |
| Text + image messages | Live | Image via chat media presign |
| Socket.IO realtime | Live | join/leave, typing, send, presence, live comment rooms |
| Piece-scoped inquiries | Deferred | Unregistered blueprint |

**Conversation status:** `open` \| `pending` \| `closed`.

### 3.13 Addresses — **Live**

| Feature | Status | Notes |
|---|---|---|
| Multi-address book | Live | Labels, map lat/lng optional, full manual fields |
| Default address | Live | First address auto-default |
| Snapshot into orders | Live | Later edits don’t change past orders |

### 3.14 Orders / checkout — **Live with stub payment**

| Feature | Status | Notes |
|---|---|---|
| Shipping quote (flat) | Live | standard 500¢, express 1500, overnight 3500, free 0 |
| Collect (create order) | Live | Tax 8.25% on artwork only; piece → `reserved` |
| Confirm payment | **Stub / seam** | No `STRIPE_SECRET_KEY` → auto-pay (`devMode`); key set → **501** |
| Ship / complete / cancel | Live | Strict transitions |
| Buyer orders / seller sales history | Live | |
| Stripe Payments + Connect + escrow | Deferred | See Phase 1 marketplace PRD |

**Order status machine:**
```
pending_payment → paid → shipped → completed
       ↓            ↓
   cancelled    cancelled
```
- Cancel from `pending_payment` or `paid` restores piece to `live` if `reserved`/`sold`.
- Seller moves `paid → shipped` and `shipped → completed`.
- “Successful purchase” counts use statuses `paid` | `shipped` | `completed`.

### 3.15 Cross-cutting platform concerns — **Live**

| Concern | Behavior |
|---|---|
| Rate limiting | Redis; OTP locks; username check / register / username-change caps; fail-open if Redis down |
| Privacy (`can_view_content`) | Private profile content grids require owner or **accepted** follower |
| Blocks | Either-way block → 404 profile, forbid follow/DM |
| Soft deletes | Pieces/posts use `deleted_at` |
| CORS + cookies | Credentialed CORS for frontend origin |
| Health | `GET /` + S3 config booleans |

---

## 4. User roles & permissions (conceptual)

| Concept | Reality in code |
|---|---|
| `role` (artist / collector / enthusiast) | Preference / primary interest only |
| `sellerEnabled` | Gates creating/updating **for-sale** listings |
| `onboardingComplete` + `emailVerified` | Required for content create, social mutations, checkout, series/collections mutate, DMs send |
| `profileVisibility` | Public content vs locked header + gated grids |
| `messagePermission` | Who may start a conversation |

Anyone who completed onboarding can post, follow, like, save, comment, and buy (subject to seller listing rules). Selling for money requires seller mode.

---

## 5. Complete end-to-end flows

### 5.1 Sign up (email OTP)

```
Client                         API                         Redis/SES/DB
  |                              |                              |
  |-- POST /auth/otp/generate -->|                              |
  |   { email }                  |-- store OTP (5m TTL) ------->|
  |                              |-- send email (SES) --------->|
  |<- 200 -----------------------|                              |
  |                              |                              |
  |-- POST /auth/otp/verify ---->|  peek OTP (non-consuming)    |
  |   { email, otp }             |                              |
  |<- 200 -----------------------|                              |
  |                              |                              |
  |-- GET /auth/username/check ->|  normalize + availability    |
  |                              |                              |
  |-- POST /auth/register ------>|  consume OTP                 |
  |   { username, name, email,  |  create User (emailVerified) |
  |     password, otp, phone? }  |  create Redis session        |
  |                              |  create RefreshToken         |
  |<- accessToken + user --------|  Set-Cookie refreshToken     |
  |   onboardingComplete: false  |                              |
```

**Constraints:** OTP 30s lock between sends, max 3/min; register rate-limited per IP; username rules `[a-z0-9._]`, max 30, blocklist.

### 5.2 Login / session refresh / logout

```
Login:   POST /auth/login { username|email, password }
         → accessToken + refreshToken cookie + Redis session

Refresh: POST /auth/refresh  (cookie only)
         → rotate refresh token + new accessToken + new session

Logout:  POST /auth/logout   (cookie) → revoke this refresh + session
Logout*: POST /auth/logout-all (Bearer) → revoke all sessions/tokens
```

Access JWT: HS256, claims `sub` + `sessionId`, short-lived (default ~15 min).  
Refresh cookie: HttpOnly, SameSite=Lax, Secure in production; long-lived until logout.  
If Redis is down during auth middleware checks, JWT may be trusted (fail-open).

### 5.3 Password reset

```
1. POST /auth/forget-password { email }
   → if user exists: hashed token in DB (1h), email FRONTEND_URL/reset-password?token=...
   → always generic success message (no email enumeration)

2. POST /auth/reset-password { token, newPassword }
   → update password, delete reset tokens, revoke ALL sessions
```

Authenticated change: `PATCH /user/me/password` → revoke others, reissue this device.

### 5.4 Onboarding

```
1. PATCH /user/me/role                    { role: artist|collector|enthusiast }
2. POST  /user/me/onboarding/preferences  { mediums[], styles[], themes[] }  // ≥3 each
3. POST  /user/me/onboarding/photos       { profilePhotoUrl?, coverPhotoUrl? } | { skip: true }
4. POST  /user/me/onboarding/complete     → onboardingComplete = true
```

Until step 4 succeeds, `onboarding_required` routes return forbidden.

### 5.5 Enable selling & list a piece

```
1. POST /user/me/seller/enable { location } | { useProfileLocation: true }

2. POST /media/presign { purpose: "piece", contentType }
   → PUT bytes to presignedPutUrl (or local fallback)
   → keep returned `url`

3. POST /pieces {
     title, mediaUrl, mediaType,
     medium, dimensions, shippingRegion,   // required when isForSale
     isForSale: true, priceCents (≥100),
     caption?, materials?, styleTags?, yearCreated?, ...
     status?: "draft"|"live"
   }
```

Disable seller is **blocked** (never destructive) while live for-sale listings or in-progress sales exist.

### 5.6 Publish a Scene and link it to a Piece

```
1. Presign purpose "post" → upload
2. POST /posts { mediaUrl, mediaType: image|video, caption?, linkedPieceId? }
3. Optional: GET /pieces/:id/related-posts
```

### 5.7 Follow (public vs private)

**Public target**
```
POST /users/:username/follow
→ Follow.status = accepted
→ notify type "follow"
→ { following: true, requested: false }
```

**Private target**
```
POST /users/:username/follow
→ Follow.status = pending
→ notify type "follow_request"
→ { following: false, requested: true }

Owner:
  GET  /users/follow-requests
  POST /users/follow-requests/:username/accept  → accepted + notify requester
  POST /users/follow-requests/:username/decline → delete request

DELETE /users/:username/follow cancels pending or unfollows accepted
```

Private accounts: non-approved viewers get locked profile header; pieces/posts/series/followers/following grids return **403**.

### 5.8 Block

```
POST /users/:username/block
→ delete mutual follows (any direction, including pending)
→ subsequent profile GET → 404 between the pair
→ follow / new conversation → 403/404
→ existing likes/comments/saves are NOT purged
```

### 5.9 Like / save / comment

```
POST|DELETE /pieces|:id/like   /posts|:id/like
POST|DELETE /pieces|:id/save   /posts|:id/save
POST        /pieces|:id/comments { body }   (onboarding)
GET         /pieces|:id/comments?cursor&limit  (public)
```

Each mutating social action notifies the content owner (not for self).  
Quick **Save** is distinct from **Collections** (named folders).

### 5.10 Feeds

```
GET /feed/following   // onboarding — accepted follows + self
GET /feed/explore     // optional auth — global live; ?medium=painting|video|...
GET /feed/for-you     // onboarding — explore clone, data.stub = true
```

All cursor-paginated (`cursor`, `limit` default 20 max 50). Items are `{ type: "piece"|"post", ...engagement fields }`.

### 5.11 Nearby discovery

```
PATCH /user/me { latitude, longitude }   // both-or-neither
GET /users/nearby?lat=&lng=&radiusKm=50&limit=20
→ sellers with sellerEnabled + coords, ordered by haversine distanceKm
```

### 5.12 Series

```
POST /series { name, pieceIds? }
PATCH /series/:id { name?, pieceOrder? }
POST /series/:id/pieces { pieceId }
DELETE /series/:id/pieces/:pieceId
GET /series/:id
GET /users/:username/series   // public: pieceCount > 1 only
GET /user/me/series           // all own series
```

### 5.13 Collections

```
POST /collections { name }
POST /collections/:id/items { targetType: piece|post, targetId }
GET  /collections | /collections/:id
PATCH/DELETE collection; DELETE item by targetType/targetId
```

### 5.14 Direct messages (REST + Socket.IO)

**Start thread**
```
POST /conversations { username, message? | imageUrl? }
→ respects messagePermission + blocks
→ status open if recipient already follows sender, else pending (requests folder)
→ reuse existing non-closed thread when present
```

**Manage**
```
GET  /conversations              // open inbox
GET  /conversations/requests     // pending
POST /conversations/:id/accept | /decline
POST /conversations/:id/messages // reply; recipient reply to pending auto-accepts
PATCH /conversations/:id/read
```

**Realtime (Socket.IO `?token=accessToken`)**

| Client → server | Server → client |
|---|---|
| `conversation:join` / `leave` | `message:new`, `message:error` |
| `typing:start` / `stop` | `typing:*`, `conversation:read` |
| `message:send` | `presence:update` |
| `target:join` / `leave` (piece/post rooms) | `comment:new` |

Presence: Redis key `presence:<userId>` TTL ~45s.

### 5.15 Checkout & order fulfillment (current)

```
Buyer                          API                         Seller
  |                              |                           |
  |-- GET shipping-quote ------->|                           |
  |<- methods (flat cents) ------|                           |
  |                              |                           |
  |-- POST /pieces/:id/collect ->|                           |
  |   { addressId, shippingMethod}|                          |
  |                              | order pending_payment     |
  |                              | piece → reserved          |
  |                              | address snapshotted       |
  |<- order + clientSecret:null -|                           |
  |                              |                           |
  |-- POST /orders/:id/confirm ->|                           |
  |                              | if no Stripe key:         |
  |                              |   order → paid            |
  |                              |   piece → sold            |
  |                              |   notify both (purchase)  |
  |<- paid + devMode:true -------|                           |
  |                              | if Stripe key set: 501    |
  |                              |                           |
  |                              |<-- PATCH status shipped --|
  |                              |<-- PATCH status completed-|
```

**Money today:** `artworkCents` + `shippingCents` + `taxCents` (8.25% of artwork) = `totalCents`. No real capture, no Connect payout, no escrow hold/release.

**Planned next (not in this codebase yet):** see Art Marketplace Phase 1 PRD — Stripe pay-in, manual shipping ops, collector confirm receipt, Connect payout.

### 5.16 Notifications & devices

```
App launch / login:
  POST /user/me/devices { platform, pushToken }

Activity:
  GET /notifications
  GET /notifications/unread-count
  PATCH /notifications/:id/read
  POST /notifications/read-all

Logout:
  DELETE /user/me/devices { pushToken }
```

Push preference keys gate FCM only; in-app rows are always written for supported types.

### 5.17 Profile & content browsing

```
GET /user/:username                 // optional auth; private lock header
GET /users/:username/pieces
GET /users/:username/pieces/for-sale
GET /users/:username/posts
GET /users/:username/series
GET /users/:username/followers|following
GET /user/me                        // full private profile
GET /user/me/saved/pieces|posts
```

Renamed usernames may return `redirectToUsername` via history.

---

## 6. Data model map (high level)

| Model | Purpose |
|---|---|
| `User` | Identity, profile, onboarding, seller, privacy, geo, prefs |
| `Account` | OAuth provider links (unused by live auth) |
| `Session` / `RefreshToken` | Session bookkeeping (live path: Redis + RefreshToken) |
| `PasswordResetToken` | Email reset |
| `UsernameHistory` | Old handles + reservation window |
| `Piece` / `Post` | Artwork vs Scenes |
| `Follow` / `Like` / `Comment` / `Save` | Social graph & engagement |
| `Collection` / `CollectionItem` | Named save folders |
| `Series` / `SeriesPiece` | Ordered piece groups |
| `Notification` / `Device` | Activity + push tokens |
| `Conversation` / `ChatMessage` | 1:1 DMs |
| `Inquiry` / `InquiryMessage` | Deferred piece threads (tables may exist) |
| `Address` | Shipping book |
| `Order` / `OrderItem` | Checkout lifecycle |
| `Block` | Block graph |

---

## 7. Module map (code)

| Module | Responsibility |
|---|---|
| `auth` | OTP, register, login, refresh, password reset, username check |
| `sessions` | Redis sessions + refresh token DAO |
| `user` | Profile, onboarding, seller, devices, addresses wiring, public profile |
| `media` | Presign (+ local storage helpers) |
| `pieces` | Artwork CRUD, collect, shipping quote |
| `posts` | Scenes CRUD |
| `social` | Follow/requests, block, like, save, comments |
| `feeds` | Following / explore / for-you |
| `series` | Series CRUD-ish |
| `collections` | Saved folders |
| `notifications` | Activity feed |
| `chat` | REST + Socket.IO DMs |
| `orders` | Confirm / patch / shipping rates |
| `addresses` | Address book DAO/controller |
| `inquiries` | **Unregistered** piece messaging |

Shared: models, JWT, Redis, S3, username utils, SES/FCM, rate limits, error handler, Socket.IO instance.

---

## 8. Explicit gaps vs product intent

Use this when planning the next sprint — these are **not** bugs; they are known seams.

| Area | Current state | Intended direction |
|---|---|---|
| Payments | Dev auto-confirm; 501 if Stripe key present | Stripe Payments (marketplace PRD) |
| Payouts / escrow | None | Stripe Connect + release after receipt confirm |
| Shipping | Flat quote table | Manual ops tracking first; carriers later |
| For You | Stub over explore | Personalized ranking |
| OAuth | Schema only | Google (and possibly others) |
| Inquiries | Unregistered | Prefer Conversations; or re-enable piece threads |
| Series delete | No endpoint | Add if product needs it |
| Daily digest | Prefs only | Needs a scheduler/worker |
| Dispute / refunds | Cancel only restores listing | Marketplace dispute states in PRD |

---

## 9. Suggested mental model for clients

1. **Auth shell** — OTP → register/login → refresh cookie → Bearer on mutations.  
2. **Onboarding gate** — role + tastes + photos → complete before publish/social/checkout.  
3. **Two content types** — Pieces (collectible art) and Scenes/`posts` (process media).  
4. **Social privacy** — public instant follow vs private request; blocks are hard walls.  
5. **Discovery** — Following + Explore (+ stub For You) + Nearby sellers.  
6. **Commerce (today)** — seller mode → list live for-sale → collect → confirm (dev pay) → ship → complete.  
7. **Messaging** — Instagram-style DMs + requests; Socket.IO for live feel.  
8. **Saves** — quick save *and* named collections.

For exact payloads, status codes, and Postman setup, use **`docs/API.md`**. For future pay/escrow/shipping ops, use **`Art-Marketplace-Phase1-PRD.md`**.

---

*Generated from the implemented backend as of the current repository state (blueprints, models, and controllers under `src/`). Update this document when major features ship.*
