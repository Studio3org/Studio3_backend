# API Documentation (Postman-ready)

For the **Studiothree Discover** backend. Use this doc to build and test requests in Postman and to integrate from mobile (iOS/Android) or web clients.

**Terminology:** UI **Scenes** map to the API `posts` resource (`/api/posts`, feed `type: "post"`, presign `purpose: "post"`).

---

## Postman setup

### 1. Environment variables

Create a Postman **Environment** (e.g. "Backend Local") with:

| Variable      | Initial / Current value   | Description                    |
|---------------|----------------------------|--------------------------------|
| `baseUrl`     | `http://localhost:9000`    | API base URL (no trailing `/`) |
| `accessToken` | *(leave empty)*            | Set automatically after login/register (see below) |

Use `{{baseUrl}}` and `{{accessToken}}` in requests.

### 2. Saving the access token

After **Login** or **Register**, the response body contains `data.accessToken`. To reuse it for protected routes:

1. Open the **Login** (or **Register**) request in Postman.
2. Go to the **Tests** tab.
3. Add:

```javascript
var json = pm.response.json();
if (json.data && json.data.accessToken) {
  pm.environment.set("accessToken", json.data.accessToken);
}
```

4. Run Login/Register; `accessToken` will be set in your environment. Protected requests that use `Authorization: Bearer {{accessToken}}` will then work.

### 3. Cookies (refresh / logout)

The server sets an httpOnly cookie `refreshToken` on **Login**, **Register**, and **Refresh**. Postman sends stored cookies automatically for the same domain.

- **Refresh:** Send a **POST** to `/api/auth/refresh` with no body and no auth header. If you previously called Login/Register from the same Postman session, the `refreshToken` cookie will be sent and you'll get a new `accessToken` (and new cookie).
- **Logout / Logout-all:** Same idea: cookie is sent automatically if it was set earlier.

### 4. Suggested flow for testing

1. **GET** `{{baseUrl}}/` → Health check.
2. **POST** `{{baseUrl}}/api/auth/login` (or register) → Get token; use the Tests script above to set `accessToken`.
3. **POST** `{{baseUrl}}/api/auth/refresh` (no headers, cookies sent automatically) → New access token.
4. **POST** `{{baseUrl}}/api/auth/logout` or **POST** `{{baseUrl}}/api/auth/logout-all` (with Bearer token for logout-all).

---

## Response format

**Success:**

```json
{
  "success": true,
  "message": "<message>",
  "data": { ... }
}
```

**Error:**

```json
{
  "success": false,
  "message": "<error message>"
}
```

Common status codes: `200`/`201` success, `400` bad request, `401` unauthorized, `403` forbidden, `404` not found, `409` conflict, `429` too many requests, `500` server error, `501` not implemented (payment provider).

**Protected routes:** Send header:

```http
Authorization: Bearer {{accessToken}}
```

**Optional-auth routes:** work without a token, but return viewer-specific fields (`isLiked`/`isSaved`/`isFollowing`) when a valid token is sent.

---

## Endpoints (copy into Postman)

### Health

**GET** `{{baseUrl}}/` → running banner plus S3 config booleans (no secret values):

```json
{
  "message": "Studiothree Discover API running",
  "s3": {
    "configured": true,
    "bucketSet": true,
    "accessKeySet": true,
    "secretKeySet": true,
    "publicBaseUrlSet": true
  }
}
```

Production should run: `gunicorn -k gevent -w 1 wsgi:app` (Python 3.12). Sync/`-w 4` and eventlet are not supported for Socket.IO.

---

### Auth – OTP generate / resend

**POST** `{{baseUrl}}/api/auth/otp/generate` and `{{baseUrl}}/api/auth/otp/resend` — body `{ "email" }`. `429` if rate/resend limit exceeded. `502` if the verification email could not be sent. `503` if the OTP store is temporarily unreachable.

### Auth – OTP verify

**POST** `{{baseUrl}}/api/auth/otp/verify` — body `{ "email", "otp" }`. Check-only: does not consume the code or create an account (registration still does the real, single-use check). `400` if the code is wrong/expired. Used by the sign-up wizard to validate the code before advancing past the OTP screen.

### Auth – Username check

**GET** `{{baseUrl}}/api/auth/username/check?username=maya_art&for_user_id=me` — `for_user_id=me` (optional, requires Bearer) excludes the current user when changing username.

```json
{ "data": { "available": true, "normalized": "maya_art", "reason": null, "message": "Username is available.", "suggestions": [] } }
```

### Auth – Register

**POST** `{{baseUrl}}/api/auth/register`

```json
{ "username": "maya_art", "name": "John Doe", "email": "user@example.com", "password": "securePassword123", "otp": "123456", "phone": "+12145551234" }
```

`phone` is optional (stored but not required — older clients omitting it still work). Sets cookie `refreshToken`. Response:

```json
{
  "data": {
    "accessToken": "eyJ...",
    "user": {
      "username": "maya_art", "name": "John Doe", "email": "user@example.com", "phone": "+12145551234",
      "emailVerified": true, "onboardingComplete": false, "role": null, "sellerEnabled": false,
      "followersCount": 0, "followingCount": 0, "piecesCount": 0, "savesCount": 0, "collectedCount": 0,
      "isFollowing": false, "savedPieces": []
    }
  }
}
```

`collectedCount` is the number of pieces this user has successfully bought (orders in `paid`/`shipped`/`completed` status — see [Orders](#orders--checkout--addresses)); `pending_payment`/`cancelled` orders don't count. The `user` object also includes legacy alias keys the client's fallback parser accepts: `following`, `followers`, `pieces`, `saves`, `collected`, `isSeller`, `saved` (mirroring the canonical keys). Errors: `400` (validation/invalid OTP), `409` (username/email taken).

### Auth – Login

**POST** `{{baseUrl}}/api/auth/login`

```json
{ "username": "maya_art", "password": "securePassword123" }
```

The `username` field accepts **either a username or an email address** (detected by the presence of `@`). Example with email:

```json
{ "username": "maya@example.com", "password": "securePassword123" }
```

Same response shape as Register. Errors: `400` (missing fields), `401` (invalid credentials).

### Auth – Refresh / Logout / Logout-all

- **POST** `/api/auth/refresh` — cookie only, no body. Returns new `accessToken` + refresh cookie. `401` if cookie missing/invalid/expired.
- **POST** `/api/auth/logout` — cookie only. Clears refresh cookie.
- **POST** `/api/auth/logout-all` — Bearer required. Revokes all sessions. `401` if not authenticated.

### Auth – Forget / Reset password

- **POST** `/api/auth/forget-password` — `{ "email" }`.
- **POST** `/api/auth/reset-password` — `{ "token", "newPassword" }`. `400` if token invalid/expired or `newPassword` missing.

---

## User & profile

### Get me / Update me (protected)

**GET** `{{baseUrl}}/api/user/me` — Bearer required. Full profile: `username`, `name`, `email`, `phone`, `bio`, `location`, `pronouns`, `mediums` (array — alias for `tastePreferences.mediums`, see below), `profilePhotoUrl`, `coverPhotoUrl`, `banner` (computed "magnum opus" display object, see below), `role`, `sellerEnabled`, `onboardingComplete`, `emailVerified`, `canChangeUsername`, `tastePreferences`, `lastUsernameChangeAt`, counts (`followersCount`/`followingCount`/`piecesCount`/`savesCount`/`collectedCount`), `isFollowing` (always `false` for self), `savedPieces` (first 20, enriched — see [Pieces](#pieces)), plus private-only editing fields `bannerTargetType`/`bannerTargetId`/`bannerAutoRule`, `messagePermission`, `profileVisibility`, `notificationPreferences`. Also includes the legacy alias keys `following`/`followers`/`pieces`/`saves`/`collected`/`isSeller`/`saved` for backward-compatible client parsing.

**PATCH** `{{baseUrl}}/api/user/me` — body (all optional): `{ "name"?, "bio"? (max 250 chars), "location"?, "phone"?, "pronouns"?, "mediums"?: [] (writes into tastePreferences.mediums, merging — doesn't clobber styles/themes), "profilePhotoUrl"?, "coverPhotoUrl"?, "latitude"?, "longitude"?, "bannerTargetType"?: "piece"|"post", "bannerTargetId"?, "bannerAutoRule"?: "most_saved"|"most_recent"|"none", "messagePermission"?: "everyone"|"following"|"no_one", "profileVisibility"?: "public"|"private" }`.

- `latitude`/`longitude` — both-or-neither (400 if only one is sent); used for [seller "near me" discovery](#geo-near-me-discovery).
- `phone` — updated directly, no re-verification (phone isn't used for login/auth in this app). Send `""`/`null` to clear it.
- `bannerTargetType`+`bannerTargetId` — manually pin a specific piece/post as the profile banner ("magnum opus"); validated against ownership (404 if not yours). Send both as `null`/omit to clear the manual pin and fall back to `bannerAutoRule`.
- `bannerAutoRule` — when no manual pin is set, the banner is **computed at read time** (no background job materializes it): `"most_recent"` picks the newest piece/post; `"most_saved"` picks whichever has the most saves (falls back to no banner if nothing has been saved yet); `"none"` shows no banner.
- `messagePermission` / `profileVisibility` — see [Privacy & blocking](#privacy--blocking) below.

**PATCH** `{{baseUrl}}/api/user/me/password` (protected) — body `{ "currentPassword", "newPassword" }` (min 8 chars). `401` if `currentPassword` is wrong. Revokes all other sessions on success (same effect as logout-all) — the caller will need to log in again elsewhere.

**Change email** (protected, two-step — the new address must be verified before it's written, mirroring registration's verify-then-write flow):
- **POST** `{{baseUrl}}/api/user/me/email/request-change` — body `{ "newEmail" }`. `409` if another account already uses it. Sends a 6-digit OTP (5 min TTL) to `newEmail` — the *old* email is untouched until confirmed.
- **POST** `{{baseUrl}}/api/user/me/email/confirm-change` — body `{ "newEmail", "otp" }`. `400` if the OTP is wrong/expired, `409` if the address was claimed by someone else in the meantime. On success, `User.email` is swapped and `emailVerified` stays `true`.

### Change username / Public profile

- **PATCH** `/api/user/me/username` — `{ "username" }`. 30-day cooldown between changes.
- **GET** `/api/user/:username` — Optional Bearer (populates `isFollowing`; omitted/anonymous gets `isFollowing: false`). Public subset only (no `email`/`phone`/`tastePreferences`/`savedPieces`). Supports `redirectToUsername` for renamed handles. `404` if either party has blocked the other. If the target's `profileVisibility` is `"private"` and the viewer isn't the owner or an accepted follower, returns a locked-header shape instead: `{ "username", "name", "profilePhotoUrl", "profileVisibility": "private", "isFollowing", "followRequestPending" }`. Otherwise (public account, or private + owner/accepted-follower) the full public subset is returned with a `followRequestPending` field added (`false` for the owner). `followRequestPending: true` means the viewer already sent a follow request awaiting the owner's approval — client should render a "Requested" button state instead of "Follow".

### Instagram-style private accounts & follow requests

- `profileVisibility` (on `PATCH /api/user/me`) — `"public"` (default) or `"private"`.
- **Public accounts**: `POST /api/users/:username/follow` follows immediately (`{ "following": true, "requested": false }`), unchanged from before.
- **Private accounts**: following requires the owner's approval. `POST /api/users/:username/follow` creates a `pending` request instead (`{ "following": false, "requested": true }`) and notifies the owner (`type: "follow_request"`). Calling it again while pending/accepted is a no-op that just returns the current state. `DELETE /api/users/:username/follow` (unfollow) also cancels a still-pending request.
- Private accounts also hide their pieces/posts/series grids from anyone who isn't the owner or an accepted follower — `GET /api/users/:username/pieces`, `.../pieces/for-sale`, `.../posts`, `.../series` all now accept an optional Bearer token and return `403` ("This account is private.") for non-approved viewers of a private account. Public accounts are unaffected — those endpoints stay fully open.
- **Managing requests (target user only):**

| Method | URL | Notes |
|--------|-----|-------|
| GET | `/api/users/follow-requests` | List pending requests to follow you (`username`, `name`, `profilePhotoUrl`, `requestedAt`) |
| POST | `/api/users/follow-requests/:username/accept` | Approve — flips the request to an accepted follow, notifies the requester |
| POST | `/api/users/follow-requests/:username/decline` | Reject — deletes the pending request, no notification |

### Privacy & blocking

- `messagePermission` (on `PATCH /api/user/me`) controls who can start a [conversation](#conversations--direct-messages) with you: `"everyone"` (default, Instagram-style message requests — see Conversations), `"following"` (only people you already follow — `403` otherwise, no request fallback), `"no_one"` (`403` always).
- **Blocked accounts:**

| Method | URL | Notes |
|--------|-----|-------|
| GET | `/api/users/blocked` | List accounts you've blocked |
| POST | `/api/users/:username/block` | Block a user |
| DELETE | `/api/users/:username/block` | Unblock |

Blocking (a) deletes any existing follow relationship (including a pending request) between the two accounts in either direction, (b) makes `POST /api/users/:username/follow` and `POST /api/conversations` (new threads) return `403`/`404` between the two accounts, (c) makes `GET /api/user/:username` return `404` between the two accounts. Existing likes/comments/saves are **not** retroactively removed.

### Onboarding (protected)

| Method | URL | Body |
|--------|-----|------|
| PATCH | `/api/user/me/role` | `{ "role": "artist" \| "collector" \| "enthusiast" }` |
| POST | `/api/user/me/onboarding/preferences` | `{ "mediums": [], "styles": [], "themes": [] }` (3+ each) |
| POST | `/api/user/me/onboarding/photos` | `{ "profilePhotoUrl"?, "coverPhotoUrl"? }` or `{ "skip": true }` |
| POST | `/api/user/me/onboarding/complete` | — |

### Seller mode & analytics (protected)

| Method | URL | Body |
|--------|-----|------|
| POST | `/api/user/me/seller/enable` | `{ "location", "useProfileLocation"?: true }` |
| POST | `/api/user/me/seller/disable` | — see deactivation rules below |
| GET | `/api/user/me/seller` | — |
| GET | `/api/user/me/seller/analytics` | — |

**Seller deactivation is blocked, never destructive** — `POST /api/user/me/seller/disable` checks state first:
- Any piece with `isForSale: true` and `status: "live"` → `400` "Remove your active listings before disabling seller mode." (listings are never auto-delisted/deleted to force this through).
- Any order with status `pending_payment`/`paid`/`shipped` where the caller is the seller → `400` "You have in-progress sales — complete or cancel them before disabling seller mode."
- Otherwise succeeds. Sold/delisted pieces stay visible in the pieces grid, order history (`/me/sales`) stays accessible, and re-enabling later needs no re-setup — all already true without any extra logic since those aren't gated by `sellerEnabled`.

**Seller analytics response:**
```json
{ "data": { "savesCount": 42, "likesCount": 128, "inquiriesCount": 7, "salesCount": 3, "period": "all_time" } }
```
`savesCount`/`likesCount` aggregate across all of the caller's pieces. `inquiriesCount` still counts rows in the deferred inquiries tables (legacy field — the `/api/inquiries` HTTP API is **not** registered; messaging is via [Conversations](#conversations--direct-messages)). `salesCount` is the number of completed sales (orders in `paid`/`shipped`/`completed` status, see [Orders](#orders--checkout--addresses)).

### Saved pieces / Saved scenes (protected)

- **GET** `{{baseUrl}}/api/user/me/saved/pieces` — lightweight list (batched): base piece fields + `author` (`isFollowing` always `false` here), `likeCount`, `isLiked`, `isSaved: true`. `commentCount` is `0`; `series`/`relatedPosts` are empty — use detail GET for full enrichment.
- **GET** `{{baseUrl}}/api/user/me/saved/posts` — same lightweight pattern for scenes.

### Devices (push notifications, protected)

- **POST** `{{baseUrl}}/api/user/me/devices` — body `{ "platform": "ios"|"android"|"web", "pushToken": "..." }`. Upserts by token — a token belongs to one app install, so registering it under a new user reassigns it (handles device-sharing/logout-login).
- **DELETE** `{{baseUrl}}/api/user/me/devices` — body `{ "pushToken" }`. Deletes the token registration (call on logout).

### Notification preferences (protected)

**PATCH** `{{baseUrl}}/api/user/me/notification-preferences` — partial update, deep-merged into the existing settings (per-key, not a full replace):
```json
{ "push": { "like": false }, "dailyDigest": { "enabled": true, "time": "18:00" } }
```
Defaults (if never set): all `push` types `true`, `dailyDigest.enabled: false`. `push.<type>` (`follow|follow_request|like|save|comment|inquiry|purchase`) gates whether that [notification](#notifications) also sends a device push — the in-app activity-feed row is always written regardless. **`dailyDigest` is a stored setting only** — no scheduled job reads or sends it yet (no task scheduler exists in this backend); don't treat it as a working feature until that lands.

### Addresses (protected)

Saved shipping address book (Zomato/Swiggy-style — multiple labeled addresses, map-pick lat/lng + full manual entry; the backend never geocodes, the client resolves the address and sends it fully formed).

| Method | URL | Body |
|--------|-----|------|
| GET | `/api/user/me/addresses` | — (sorted default-first, then newest) |
| POST | `/api/user/me/addresses` | see below |
| PATCH | `/api/user/me/addresses/:id` | any subset of the same fields |
| DELETE | `/api/user/me/addresses/:id` | — |
| POST | `/api/user/me/addresses/:id/default` | — (unsets any prior default) |

**Create/update body:**
```json
{
  "label": "Home", "firstName": "Jane", "lastName": "Doe", "phone": "+12145551234",
  "line1": "123 Main St", "line2": "Apt 4B", "city": "Dallas", "state": "TX", "zip": "75201",
  "country": "US", "latitude": 32.7767, "longitude": -96.7970, "isDefault": true
}
```
`firstName`, `lastName`, `phone`, `line1`, `city`, `state`, `zip` are required on create. The first address a user saves is automatically made default.

### Geo "near me" discovery

**GET** `{{baseUrl}}/api/users/nearby?lat=32.78&lng=-96.80&radiusKm=50&limit=20` — Optional Bearer. Returns sellers (`sellerEnabled: true` with lat/lng set) near the given point, ordered by distance:

```json
{ "data": { "items": [{ "...": "same shape as user_to_dict", "distanceKm": 4.2 }] } }
```

Distance computed via the haversine formula in raw SQL (no PostGIS on this Postgres instance) — a full-table scan per call, fine at hundreds/low-thousands of sellers. `radiusKm` defaults to 50, `limit` defaults to 20 (max 50).

---

## Media

**POST** `{{baseUrl}}/api/media/presign` (Bearer)

```json
{ "purpose": "profile" | "cover" | "piece" | "post" | "chat" | "event", "contentType": "image/jpeg", "pieceId": "optional", "postId": "optional", "eventId": "optional" }
```

`purpose` decides the key shape. `profile` and `cover` are the signed-in user's avatar and **profile banner** — one fixed object each, so a new upload replaces the old one. Everything else is addressed by id and gets its own object; use `event` for an event's cover flyer, never `cover`, which would overwrite the host's profile banner and then serve it as the event image.

`contentType`: `image/jpeg`, `image/png`, `image/webp` (max 20MB), or `video/mp4` (max 100MB) — video is supported for any purpose. Response: `{ "presignedPutUrl", "url", "key", "devMode" }` — upload to `presignedPutUrl`, then pass `url` when creating/updating content. `devMode: true` when S3 isn't configured (local dev) — `presignedPutUrl` is `null` and `url` is a placeholder.

---

## Pieces & Scenes

Create/edit routes require completed onboarding (`onboardingComplete: true`). Detail (`GET /:id`) routes accept an **optional** Bearer token (`optional_auth`) — send one for viewer-specific `isLiked`/`isSaved`/`author.isFollowing`; omit for anonymous (those fields default to `false`).

### Pieces

| Method | URL | Auth | Notes |
|--------|-----|------|-------|
| POST | `/api/pieces` | Bearer | Create; sale fields require seller mode |
| GET | `/api/pieces/:id` | Optional Bearer | Enriched detail |
| PATCH | `/api/pieces/:id` | Bearer | Edit, toggle sale |
| GET | `/api/pieces/:id/related-posts` | — | Scenes linked to piece |
| GET | `/api/pieces/:id/comments` | — | Cursor-paginated comment thread |
| GET | `/api/pieces/:id/shipping-quote` | Optional Bearer | See [Orders](#orders--checkout--addresses) |
| POST | `/api/pieces/:id/collect` | Bearer + onboarding | Checkout — see [Orders](#orders--checkout--addresses) |
| GET | `/api/users/:username/pieces` | — | Profile Work tab |
| GET | `/api/users/:username/pieces/for-sale` | — | For Sale tab |
| GET | `/api/user/me/saved/pieces` | Bearer | See [User](#saved-pieces--saved-scenes-protected) |

**Create piece body:**
```json
{
  "title": "Sunset Study", "mediaUrl": "https://.../abc.jpg", "mediaType": "image",
  "caption": "Oil on canvas", "medium": "painting", "isForSale": true, "priceCents": 25000,
  "dimensions": "24x36 in", "shippingRegion": "US",
  "yearCreated": 2024, "framingMounting": "Framed, ready to hang", "provenance": "Direct from artist",
  "handlingNotes": "Keep away from direct sunlight", "materials": ["oil", "canvas"], "styleTags": ["abstract"]
}
```
`yearCreated`/`framingMounting`/`provenance`/`handlingNotes` are first-class optional fields (replace the old caption-JSON workaround). `materials[]`/`styleTags[]`/`aiDisclosed`/`altText` are also accepted on create and returned on every piece response. `PATCH` accepts the same field set plus `status` (`draft|live|sold|delisted|reserved` — `reserved`/`sold` are set automatically by checkout, not client-settable in normal use).

**Get piece response** (`GET /api/pieces/:id`) — all create fields plus:
```json
{
  "status": "live", "createdAt": "...",
  "author": { "username": "maya_art", "name": "Maya", "profilePhotoUrl": "...", "isFollowing": false },
  "likeCount": 12, "commentCount": 3, "isLiked": false, "isSaved": false,
  "series": { "id": "...", "name": "Riverwalk Dream", "pieceCount": 3, "previewPieces": [...], "pieceIds": [...] },
  "relatedPosts": []
}
```
`series` is `null` if the piece doesn't belong to one. `GET /api/users/:username/pieces` / `.../pieces/for-sale` return base piece arrays without engagement enrichment (`isLiked`/`isSaved`/`author.isFollowing` absent or false). Saved lists use a **lightweight** shape — see [Saved pieces](#saved-pieces--saved-scenes-protected). The `/api/users/:username/*` list endpoints accept an optional Bearer token and return `403` if the target account is `private` and the caller isn't the owner or an accepted follower — see [Instagram-style private accounts](#instagram-style-private-accounts--follow-requests).

### Scenes (API: posts)

UI **Scenes** map to the `posts` resource. Scenes may be **image** or **video** (`mediaType`) and are not collectible (no sale fields).

| Method | URL | Auth | Notes |
|--------|-----|------|-------|
| POST | `/api/posts` | Bearer | Create scene (image or video) |
| GET | `/api/posts/:id` | Optional Bearer | Enriched detail |
| PATCH | `/api/posts/:id` | Bearer | Edit, link/unlink piece |
| GET | `/api/posts/:id/comments` | — | Cursor-paginated comment thread |
| GET | `/api/users/:username/posts` | — | Profile **Scenes** tab |
| GET | `/api/user/me/saved/posts` | Bearer | Saved scenes |

**Create scene body:** `{ "mediaUrl", "mediaType": "image"|"video", "caption"?, "linkedPieceId"? }` — `mediaType` must be `image` or `video`; `isProcess` defaults to `false` when omitted.

**Get scene response** (`GET /api/posts/:id`) — base fields (`id`, `userId`, `mediaUrl`, `mediaType`, `caption`, `isProcess`, `linkedPieceId`, `status`, `createdAt`) plus enrichment: `author`, `likeCount`, `commentCount`, `isLiked`, `isSaved`, and `piece` (full linked piece, or `null`).

---

## Social

Like/save/comment-create/follow mutation routes require Bearer + completed onboarding. Comment **reads** are public (no auth).

| Method | URL | Notes |
|--------|-----|-------|
| POST/DELETE | `/api/users/:username/follow` | Follow/unfollow — instant for public accounts, creates/cancels a pending request for private accounts (see [Instagram-style private accounts](#instagram-style-private-accounts--follow-requests)) |
| GET | `/api/users/follow-requests` | Pending requests to follow you |
| POST | `/api/users/follow-requests/:username/accept` | Approve a pending request |
| POST | `/api/users/follow-requests/:username/decline` | Reject a pending request |
| POST/DELETE | `/api/pieces/:id/like`, `/api/posts/:id/like` | Like/unlike — `404` if target missing. Response `{ "liked": true\|false, "likeCount": N }` |
| POST/DELETE | `/api/pieces/:id/save`, `/api/posts/:id/save` | Save/unsave — `404` if target missing. Response `{ "saved": true\|false, "saveCount": N }` |
| POST | `/api/pieces/:id/comments`, `/api/posts/:id/comments` | `{ "body" }` |
| GET | `/api/pieces/:id/comments`, `/api/posts/:id/comments` | Query: `cursor?`, `limit?` (default 50, max 100) |

Like/unlike and save/unsave are idempotent (re-like while already liked still returns `liked: true`). Concurrent duplicate inserts are treated as success.

**Get comments response:**
```json
{
  "data": {
    "items": [
      { "id": "uuid", "body": "Beautiful work!", "author": { "username": "alex_chen", "name": "Alex Chen", "profilePhotoUrl": "https://..." }, "createdAt": "2026-07-08T12:00:00Z" }
    ],
    "nextCursor": "2026-07-08T11:00:00+00:00"
  }
}
```
Pass `nextCursor` back as `?cursor=` to fetch the next (older) page; `null` when there are no more comments.

Follow/like/save/comment each emit a [notification](#notifications) (with push) to the target's owner — skipped for self-actions.

---

## Feeds

| Method | URL | Auth | Description |
|--------|-----|------|--------------|
| GET | `/api/feed/following` | Bearer + onboarding | Chronological pieces + scenes from followed users |
| GET | `/api/feed/explore` | Optional Bearer | Recent public pieces + scenes; `?medium=painting` filters pieces by medium; `?medium=video` returns video scenes only |
| GET | `/api/feed/for-you` | Bearer + onboarding | Stub wrapping explore (full personalization engine deferred) |

All three are cursor-paginated: `?cursor=&limit=` (default 20, max 50). Response:
```json
{
  "data": {
    "items": [{
      "type": "piece"|"post",
      "...": "base piece or scene fields",
      "author": { "username", "name", "profilePhotoUrl", "isFollowing" },
      "likeCount": 12, "commentCount": 3, "isLiked": false, "isSaved": false
    }],
    "nextCursor": "<opaque-cursor-string-or-null>",
    "stub": true
  }
}
```
`stub` is only present on **for-you** (always `true` until personalization ships). Feed items include batched `likeCount`, `commentCount`, `isLiked`, `isSaved`, and `author.isFollowing` (false when anonymous). They do **not** include full detail-only fields such as `series` / `relatedPosts` / linked `piece`. `type` is `"piece"` or `"post"`. Default `explore` (no `medium`) returns pieces and scenes merged by recency; `?medium=<piece medium>` returns pieces of that medium only; `?medium=video` returns video scenes only. Following feed only includes **accepted** follows (plus the caller). Pass `nextCursor` back as `?cursor=` for the next page; `null` means no more items.

---

## Series

Piece grouping for an artist's profile "Series" tab. Mutation routes require Bearer + completed onboarding; reads are public. A piece belongs to at most one series — adding it to a second series is rejected with `400`.

| Method | URL | Auth | Body |
|--------|-----|------|------|
| GET | `/api/users/:username/series` | — | — (only series with `pieceCount > 1` are returned) |
| GET | `/api/series/:id` | — | — |
| POST | `/api/series` | Bearer | `{ "name": "...", "pieceIds"?: ["..."] }` |
| PATCH | `/api/series/:id` | Bearer | `{ "name"?: "...", "pieceOrder"?: ["pieceId", ...] }` |
| POST | `/api/series/:id/pieces` | Bearer | `{ "pieceId": "..." }` |
| DELETE | `/api/series/:id/pieces/:pieceId` | Bearer | — |

**Series object** (list — `GET /api/users/:username/series`):
```json
{ "id": "uuid", "name": "Riverwalk Dream", "pieceCount": 3, "previewPieces": [{ "id": "...", "mediaUrl": "...", "title": "..." }] }
```

**Series detail** (`GET /api/series/:id`, and `create`/`patch`/`add-piece`/`remove-piece` responses) — same shape plus `pieceIds: ["...", "...", "..."]` (full ordered list, matches the `series` field embedded in piece detail).

---

## Notifications

General activity feed. Every entry may also trigger a push via [Devices](#devices-push-notifications-protected) (Firebase Cloud Messaging — iOS/Android/Web, sent synchronously, fails open/logs if unconfigured — never blocks the triggering action).

| Method | URL | Notes |
|--------|-----|-------|
| GET | `/api/notifications` | Bearer. Cursor-paginated (`?cursor=&limit=`, default 20 max 50) |
| PATCH | `/api/notifications/:id/read` | Bearer |
| POST | `/api/notifications/read-all` | Bearer |
| GET | `/api/notifications/unread-count` | Bearer → `{ "count": N }` |

**List response:**
```json
{
  "data": {
    "items": [{
      "id": "uuid", "type": "save"|"follow"|"follow_request"|"inquiry"|"purchase"|"like"|"comment",
      "actor": { "username", "name", "profilePhotoUrl" },
      "target": { "type": "piece"|"post"|"order"|"inquiry"|"user", "id": "uuid" },
      "payload": { "...": "type-specific pre-rendered fields" },
      "message": "saved your piece", "read": false, "createdAt": "..."
    }],
    "nextCursor": "..."
  }
}
```

Emitted automatically by: follow (or `follow_request` for a private-account request — see [Instagram-style private accounts](#instagram-style-private-accounts--follow-requests)), like, save, comment (see [Social](#social)), new chat messages (see [Conversations](#conversations--direct-messages)), and order confirmation (see [Orders](#orders--checkout--addresses)).

---

## Conversations (direct messages)

General-purpose **1:1 DMs** at `/api/conversations`. Mutations that start/reply require Bearer + onboarding; reads require Bearer (participant-only). Blocked-either-way pairs get `403`/`404` (see [Privacy & blocking](#privacy--blocking)).

**Instagram-style message requests**: a thread has three states — `open` (both sides can read/reply), `pending` (recipient does not yet follow the sender — sits in their Requests folder until accepted, declined, or replied to), `closed` (declined). See [`messagePermission`](#privacy--blocking).

| Method | URL | Body / notes |
|--------|-----|--------------|
| GET | `/api/conversations` | Primary inbox, cursor-paginated |
| GET | `/api/conversations/requests` | Incoming `pending` requests folder |
| GET | `/api/conversations/search-users` | Query `q` — find users to message |
| GET | `/api/conversations/unread-count` | `{ "count": N }` |
| GET | `/api/conversations/with/:username` | Existing open/pending thread with user, if any |
| GET | `/api/conversations/:id` | Thread + messages; auto-marks read |
| POST | `/api/conversations` | `{ "username", "message"? , "imageUrl"? }` — message or imageUrl required |
| POST | `/api/conversations/:id/messages` | `{ "body"? , "imageUrl"? }` — body or imageUrl required; seller/recipient reply to `pending` auto-accepts |
| POST | `/api/conversations/:id/accept` | Recipient only, `pending → open` |
| POST | `/api/conversations/:id/decline` | Recipient only, `pending → closed` |
| PATCH | `/api/conversations/:id/read` | Explicit mark-read |

**`POST /api/conversations`:** `403` if the recipient's `messagePermission` is `"no_one"`, or `"following"` and they don't already follow the sender. `400` if messaging yourself. Reuses a non-closed thread with `200` (`{"id", "reused": true}`) or creates with `201` (`{"id", "reused": false, "status": "open"|"pending"}`).

### Socket.IO (real-time)

Connect to the same host's Socket.IO endpoint with the access token as a query param: `?token={{accessToken}}`. Invalid/missing token → handshake rejected.

**Client → server events:**

| Event | Payload |
|-------|---------|
| `conversation:join` | `{ "conversationId" }` |
| `conversation:leave` | `{ "conversationId" }` |
| `typing:start` / `typing:stop` | `{ "conversationId" }` |
| `message:send` | `{ "conversationId", "body"?, "imageUrl"? }` |
| `target:join` / `target:leave` | `{ "targetType": "piece"|"post", "targetId" }` — live comments room |

**Server → client events:** `message:new`, `message:error`, `conversation:read`, `typing:start`/`typing:stop`, `presence:update`, and `comment:new` on target rooms. Production uses **gevent** + Redis pub/sub (`REDIS_URL`).

---

## Collections

Instagram-style saved folders for pieces and scenes. All routes require Bearer; create/mutate require onboarding.

| Method | URL | Body |
|--------|-----|------|
| GET | `/api/collections` | — list caller's collections (summary + cover) |
| POST | `/api/collections` | `{ "name" }` |
| GET | `/api/collections/:id` | — detail with enriched `items` |
| PATCH | `/api/collections/:id` | `{ "name" }` |
| DELETE | `/api/collections/:id` | — |
| POST | `/api/collections/:id/items` | `{ "targetType": "piece"|"post", "targetId" }` |
| DELETE | `/api/collections/:id/items/:targetType/:targetId` | — |

---

## Inquiries (deferred v2)

The piece-scoped `/api/inquiries` blueprint is **not registered**. Schema/migrations may still exist, but HTTP calls return **404**. Use [Conversations](#conversations--direct-messages) for messaging.

---

## Orders / Checkout / Addresses

Full checkout lifecycle **except real payment capture** — the client is still finalizing a payment gateway. See [Addresses](#addresses-protected) for the address book used at checkout.

### Shipping quote

**GET** `{{baseUrl}}/api/pieces/:id/shipping-quote` — Optional Bearer.
```json
{ "data": { "methods": [{"id":"standard","cents":500},{"id":"express","cents":1500},{"id":"overnight","cents":3500},{"id":"free","cents":0}] } }
```
Flat global rates — no carrier integration.

### Checkout (collect)

**POST** `{{baseUrl}}/api/pieces/:id/collect` — Bearer + onboarding. Body: `{ "addressId", "shippingMethod" }`.

Validates the piece is for-sale and `status == "live"` (else `409`), the caller isn't the seller (`400`), the address belongs to the caller (`404`), and the method is valid (`400`). Computes `artworkCents`/`shippingCents`/`taxCents` (flat 8.25% placeholder, server-side)/`totalCents`, creates the order, snapshots the chosen address (edits to the saved address later never change this), and reserves the piece (`status → "reserved"`, so it disappears from sale listings immediately). Response:
```json
{ "data": { "id", "status": "pending_payment", "artworkCents", "shippingCents", "taxCents", "totalCents", "shippingAddress": {...}, "items": [...], "clientSecret": null } }
```
`clientSecret` is a contract-stable placeholder — `null` today, populated once a real payment provider is wired up.

### Confirm (pay)

**POST** `{{baseUrl}}/api/orders/:id/confirm` — Bearer, caller must be the buyer, `409` unless the order is `pending_payment`.

**No payment provider configured (current default):** auto-succeeds — order → `paid`, piece → `sold`, both buyer and seller are notified (`type: "purchase"`). Response includes `"devMode": true`.

**Provider key present (not yet built):** `501 Payment provider not yet implemented.` — this is an intentional seam, not a bug; real Stripe integration lands in a future pass.

### Order management

| Method | URL | Notes |
|--------|-----|-------|
| GET | `/api/orders/:id` | Buyer or seller only |
| PATCH | `/api/orders/:id` | `{"status": "shipped"\|"completed"\|"cancelled"}` — seller-only for `shipped`/`completed` (strict forward order from `paid`), buyer-or-seller for `cancelled` (from `pending_payment`/`paid`, releases the piece back to `"live"`) |
| GET | `/api/user/me/orders` | Buyer history, cursor-paginated |
| GET | `/api/user/me/sales` | Seller history, cursor-paginated |

**Order object** (all of the above): `{ id, buyerId, sellerId, status, shippingMethod, shippingAddress, artworkCents, shippingCents, taxCents, totalCents, paymentProvider, items: [{pieceId, priceCents, quantity}], createdAt, updatedAt }`.

---

## Auctions & bidding

A piece is **either** fixed-price **or** an auction, never both. `listingType` says which, and
`POST /api/pieces/:id/collect` returns `409` on an auction piece — auctions are bought by
winning them, not by buying them outright.

### The money model, because it drives every field below

Bidding authorises money rather than taking it. Each bid places a **hold** on the bidder's
card for the **bid amount only** — shipping and tax come later. Every bidder keeps their hold
until the auction closes: being outbid by someone else does *not* release your money, because
you are still in line if the higher bid falls through. The only thing that moves your hold
mid-auction is raising your own bid.

When the auction closes, the winner's hold is **captured**. That means by the time the winner
reaches checkout the hammer price is already paid, and checkout collects only the remainder.
Clients must use `balanceDueCents`, never `totalCents`, when showing what is left to pay.

### Bid summary

Returned by the piece detail endpoint and by every endpoint below. Fields:

| Field | Meaning |
|-------|---------|
| `auctionId`, `auctionStatus` | `draft`, `live`, `closing`, `awaiting_payment`, `awaiting_winner`, `closed_sold`, `closed_reserve_not_met`, `closed_no_bids`, `needs_seller_action`, `cancelled` |
| `startingBidCents` | The artist's stated minimum. The **first bid may land exactly on this number.** |
| `highestBidCents` | `null` until someone bids. Never conflated with the starting bid. |
| `minNextBidCents` | What to prefill. Equals `startingBidCents` with no bids, else highest + increment. |
| `bidIncrementCents` | The band for the current high bid — `0` when there are no bids. Never hardcode this client-side. |
| `bidCount` | Active bids only. |
| `opensAt`, `auctionEndsAt` | ISO 8601. An event auction's window comes from the event. |
| `hasReserve`, `reserveMet` | Met/not-met only. **The reserve amount is never returned.** |
| `deliveryMode` | `ship` or `pickup` (event auctions). |
| `isHighestBidder` | Viewer-relative. |
| `isWinner` | Viewer-relative, post-close. Read from the stored winner, not from the bid ordering. |
| `awaitingPayment` | `true` only for the winner whose card was declined — drives the "add another card" prompt. Never shown to other bidders. |
| `winnerDeadlineAt` | When a declined winner loses the piece to the next bidder. `null` once settled. |
| `winningBidCents` | The hammer price, post-close. |

Increment bands: under $100 → $5 · $100–499 → $10 · $500–1,999 → $25 · $2,000–9,999 → $100 ·
$10,000+ → $500.

### Saved cards (required before the first bid)

A bid authorises money the moment it is placed, and the same card is **re-authorised weeks
later with nobody present**. There is no point in that sequence at which a card could be
collected, so it is vaulted at Stripe up front and referenced by id. Card data never reaches
this server.

| Method | URL | Notes |
|--------|-----|-------|
| POST | `/api/payments/setup-intent` | `201` with `clientSecret`, `customerId`, `ephemeralKeySecret`. Hand all three to Stripe's PaymentSheet — without the ephemeral key the sheet cannot show the customer's existing cards. |
| GET | `/api/payments/payment-methods` | `{ "paymentMethods": [{ "id", "brand", "last4", "expMonth", "expYear" }] }`. Empty (not an error) for someone who has never bid. |
| DELETE | `/api/payments/payment-methods/:id` | Ownership is checked against the caller's own Stripe customer; someone else's card answers `404`. |

The Stripe customer is created once and stored on the user, so a card saved last week is
still listed this week.

### Placing a bid

**POST** `/api/pieces/:id/bids` — Bearer + onboarding. Body:
`{ "amountCents", "paymentMethodId" }`.

`paymentMethodId` is required on a bidder's first bid on a piece and optional afterwards —
raising your own bid reuses the card already committed to that auction. Errors: `400` (bidding
on your own piece, no payment method, not an auction), `402` (the card refused the
authorisation — the bid is not placed), `409` (auction not open, bidding closed, or below
`minNextBidCents`). Returns `201` with the bid plus the full bid summary.

A bid placed in the final five minutes of a **standalone** auction pushes the close out by
five minutes, repeatedly, so nobody wins by sniping. An **event** auction never extends: it
stops dead 30 minutes before the event ends so the piece can change hands before the room
empties.

### Winning and checking out

On close the winner's hold is captured and every other bidder is released. The piece moves to
`auction_won` and the auction to `awaiting_winner`.

**POST** `/api/pieces/:id/auction-checkout` — Bearer + onboarding. Body:
`{ "addressId", "shippingMethod" }`. Same shape as `collect`, with two additions in the
response:

```json
{ "data": { "...": "...", "totalCents": 54625, "prepaidCents": 50000, "balanceDueCents": 4625 } }
```

`prepaidCents` is the hammer price already captured. `balanceDueCents` is shipping + tax, and
is what `POST /api/orders/:id/create-payment-intent` will raise its intent for. Errors: `403`
(not the winner), `409` (auction not ended, already claimed, or the winner's payment has not
gone through yet).

### When the winner's card fails

The auction goes to `awaiting_payment` rather than being voided. **Both** the winner and the
seller are notified immediately. The winner has until `winnerDeadlineAt` to fix it — **48
hours** for a standalone auction, **10 minutes** for an event one, because at an event there
is a person in the room with another card in their hand. Every runner-up keeps their hold for
exactly this window; that is what makes the fallback possible.

**POST** `/api/pieces/:id/auction/retry-payment` — Bearer. Body: `{ "paymentMethodId" }`.
Winner only (`403` otherwise). `409` if there is no payment waiting to be fixed, `402` if the
new card is declined too (the window continues). On success the sale settles exactly as a
clean close would.

If the window runs out the piece passes to the next-highest bidder, who gets their own window.
After a bounded number of attempts the auction is parked as `needs_seller_action` and every
hold is released — nothing is charged to anyone.

### Seller actions

| Method | URL | Body | Notes |
|--------|-----|------|-------|
| POST | `/api/pieces/:id/auction/extend` | `{ "extraDays": 1-3 }` | Once per auction, and only more than 3 days before it closes, so extending cannot be used to control exactly when an auction ends. Never shortens. Not available on event auctions. Bids and holds survive. |
| POST | `/api/pieces/:id/auction/cancel` | — | Withdraws a live auction. Every bid is voided, every hold released, every bidder notified. The piece is delisted. |
| POST | `/api/pieces/:id/auction/relist` | `{ "durationDays", "startingBidCents"?, "reserveCents"? }` | Only from `needs_seller_action` (reserve not met, or nobody could pay). Creates a **new** auction row; the old one stays as the record of what happened. Omitted fields inherit the previous auction's terms. |

All three are owner-only and answer `404` — not `403` — for a piece you do not own.

An auction that ends without a sale is never auto-relisted or auto-converted to a fixed price:
it waits for the seller to decide.


## Events

An event is a gathering: when, where, who is on the bill, and which work is shown there.

**Entry is free and open.** Attending is never gated — browse and detail work signed out, and
`isFree` is always `true` today. Registering exists to let someone transact, not to let them
in. Paid ticketing is deferred, so there are no ticket or price fields at all.

### Draft, then publish

Creating an event makes a **draft**: private to its host, invisible to every listing, and
listing nothing. `POST /publish` is what puts it out *and* turns its bill into real listings.
That split is the point — a host builds and rearranges a bill without work going on sale
under them.

| Method | URL | Notes |
|--------|-----|-------|
| POST | `/api/events` | `{ title, startsAt, endsAt, description?, coverMediaUrl?, category?, timezone?, venueName?, address?, latitude?, longitude? }` → `201`, status `draft`. Times are ISO 8601 UTC; `timezone` is the IANA zone the event happens in, kept so its time reads the same to everyone. |
| PATCH | `/api/events/:id` | Same fields. `409` on a published event's dates — its auctions already take their window from them and have bids against them. |
| PUT | `/api/events/:id/people` | `{ cohostUsernames?, artistUsernames? }`. A role that is sent is replaced wholesale (that is how you remove somebody); a role left out is untouched. The host is never added as their own cohost. |
| POST | `/api/events/:id/publish` | Publishes and lists the bill. Idempotent. Returns `fixedListings` / `auctionListings` counts. `409` if the event has already ended. |
| POST | `/api/events/:id/cancel` | `{ reason? }`. Takes down every listing the event created and cancels any auction properly — holds released, bidders told. Returns `cancelledAuctions` / `delisted`. |

Host-only endpoints answer **`404`, not `403`**, for someone else's event: whether a given id
is a real draft is not a stranger's to probe for.

### The bill

`mode` is how a piece appears at *this* event, and the three modes are not equivalent:

| Mode | What it does |
|------|--------------|
| `featured` | Shown only. **Nothing** about the piece's own listing changes. |
| `sale` | Sold at a fixed price at the event. |
| `bid` | Auctioned in the room, on the event's clock. |

**`sale` and `bid` end whatever listing the piece already had.** A running auction is
cancelled — every bidder's hold released, every bidder notified that the seller withdrew it.
That is irreversible, so ask first:

**GET** `/api/events/:id/pieces/:pieceId/tagging-preview` →
`{ endsAuction, endsFixedListing, activeBidCount, ownedByViewer }`. Use the real numbers in
the confirmation ("this ends the auction and refunds 4 bidders").

| Method | URL | Notes |
|--------|-----|-------|
| POST | `/api/events/:id/pieces` | `{ pieceId, mode, priceCents?, deliveryMode?, sortOrder? }`. `priceCents` required and ≥ $1 for `sale`/`bid`. `deliveryMode` defaults to `pickup` — at an event the work changes hands in the room. |
| DELETE | `/api/events/:id/pieces/:pieceId` | Removes it from the bill. Deliberately does **not** restore the listing it replaced: those bidders were already refunded and told it was over. |

**Who may do what.** The host runs the bill and may add anyone's work as `featured`. Only the
**piece's owner** may choose `sale` or `bid` — selling someone else's work, and cancelling
their auction to do it, is not a host's decision to make (`403`). An artist on the bill may
add their own work.

Adding a piece with a sale already in flight (`reserved`/`sold`/`auction_won`) answers `409`.

### Event auctions

Publishing a `bid` entry creates an auction whose window is the **event's**:

* opens at `startsAt` — bidding opens when the doors do, not when the host hits publish;
* closes at `endsAt` **minus 30 minutes**;
* **no soft close** — it stops dead so the work can be handed over before the room empties.

An event too short for that window (under ~30 minutes) is refused at publish with `400`,
because bidding would close before it opened.

Everything else about bidding is unchanged — see [Auctions & bidding](#auctions--bidding).

### RSVPs

An RSVP is **not a ticket**. Entry is free and open, so it admits nobody and charges nothing
— what it buys is that the host knows how many to expect, and that there is somebody to tell
when the event is called off. Never word it as a purchase.

| Method | URL | Notes |
|--------|-----|-------|
| POST | `/api/events/:id/rsvp` | `{ going: true\|false }` → `{ going, rsvpCount, spotsLeft, isFull }`. Idempotent both ways. `409` when a capped event is full, `400` for the host's own event, `404` for a draft. |
| GET | `/api/events/:id/attendees` | **Host-only** (`404` otherwise) — an attendee list is not public. |

Every event payload carries `rsvpCount`, `capacity`, `spotsLeft`, `isFull` and the
viewer-relative `viewerIsGoing`.

**`spotsLeft` is null, not zero, when the event is uncapped.** The two mean different things:
an uncapped event has no number to show, a full one has exactly zero.

**Capacity** is optional and null by default. Setting it caps RSVPs; the check runs under a
row lock, so two people racing for the last place serialise rather than both being told yes.
It cannot be lowered below the number already coming (`409`) — those people said yes in good
faith and there is no mechanism for choosing which to turn away. The waitlist that turns
"full" into a queue is deferred.

Cancelling an RSVP flips the row to `cancelled` rather than deleting it, so "pulled out" and
"never replied" stay distinguishable. Cancelled RSVPs are never counted and never notified.

**Cancelling an event notifies everyone who was coming** (`attendeesNotified` in the
response), alongside taking down the bill.

### QR codes and the room

**GET** `/api/events/:id/qr-codes` — host-only. Returns `eventUrl` plus one entry per work on
the bill with `{ pieceId, title, mode, priceCents, mediaUrl, url }`.

The `url` is what the code encodes: **a link, not a token**. Scanning it navigates — it
admits nobody and proves nothing. That is deliberate, and it is what lets a visitor
photograph a code, send it to someone across the room, and have that work too.

Built server-side so the code printed on the wall and the link the app resolves cannot become
two different opinions about what a share URL looks like.

### App association

The backend serves both files, at the **site root** (not under `/share` — the platforms fetch
a fixed path):

| URL | For |
|-----|-----|
| `/.well-known/apple-app-site-association` | iOS Universal Links |
| `/.well-known/assetlinks.json` | Android App Links |

Contents come from `IOS_TEAM_ID`, `IOS_BUNDLE_ID`, `ANDROID_PACKAGE` and
`ANDROID_CERT_FINGERPRINTS`. **Unset means the file associates nothing** rather than shipping
a placeholder — a file containing `REPLACE_WITH_TEAM_ID` looks configured and fails in a way
that costs an afternoon.

`ANDROID_CERT_FINGERPRINTS` is comma-separated and should list *every* signing key whose
builds must verify: release, the debug key that internally-shared APKs use, and Google's
re-signing key once the app is on Play. One key means links verify for some of your builds
and silently not others.

Both must be served over HTTPS with **no redirect** — both platforms treat a redirect as a
failure, and it is the usual reason association quietly does not work.

The `studio3://` custom scheme needs none of this and works regardless, which is what the QR
flow falls back to before a domain is pointed anywhere.

### Live lineup state

A `bid` entry in an event's `lineup` carries an `auction` object — the same shape as
[Bid summary](#bid-summary), including `highestBidCents`, `bidCount`, `auctionEndsAt` and the
viewer-relative `isHighestBidder` / `isWinner`.

It is there because the screen people look at in a room is the *event*, not each piece in
turn. Without it a lineup could only show the starting price, which stops being true the
moment somebody bids.

Every auction at an event shares the event's clock, so one countdown describes them all.

### Browsing

| Method | URL | Notes |
|--------|-----|-------|
| GET | `/api/events/browse` | The whole tab in one call: `today`, `following`, `upcoming`, `categories`. |
| GET | `/api/events?scope=` | `upcoming` (default, accepts `category`, `limit`, `offset`), `today`, `following`, `saved`. The last two need a signed-in viewer (`401`). |
| GET | `/api/events/:id` | Detail: adds `description`, `cohosts`, `artists`, `lineup`, `saveCount`, `isHost`. A draft answers `404` to anyone but its host. |
| POST | `/api/events/:id/save` | `{ saved: true|false }` → `{ saved, saveCount }`. |

"Upcoming" is keyed on **`endsAt`**, not `startsAt`: an event that began an hour ago and runs
until midnight is still happening, and dropping it the moment it started would lose exactly
the events someone browsing right now is most likely to want.

The `following` scope includes events a followed artist is **billed on**, not just ones they
host — an artist showing at someone else's gallery is usually the reason to care. Only
accepted follows count.

Categories: `workshop` · `gallery_walk` · `exhibition` · `talks_panels` ·
`demos_performances` · `studio_visit` · `popup` · `market` · `auction` · `other`.


## Quick reference

| Method | URL | Auth | Body |
|--------|-----|------|------|
| GET | `{{baseUrl}}/` | — | — |
| GET | `{{baseUrl}}/api/auth/username/check` | Optional | Query: `username`, `for_user_id=me` |
| POST | `{{baseUrl}}/api/auth/otp/generate` \| `/otp/resend` | — | `{ "email" }` |
| POST | `{{baseUrl}}/api/auth/otp/verify` | — | `{ "email", "otp" }` |
| POST | `{{baseUrl}}/api/auth/register` | — | `{ "username", "name", "email", "password", "otp", "phone"? }` |
| POST | `{{baseUrl}}/api/auth/login` | — | `{ "username", "password" }` — `username` accepts username or email |
| POST | `{{baseUrl}}/api/auth/refresh` | Cookie | — |
| POST | `{{baseUrl}}/api/auth/logout` | Cookie | — |
| POST | `{{baseUrl}}/api/auth/logout-all` | Bearer | — |
| POST | `{{baseUrl}}/api/auth/forget-password` | — | `{ "email" }` |
| POST | `{{baseUrl}}/api/auth/reset-password` | — | `{ "token", "newPassword" }` |
| GET/PATCH | `{{baseUrl}}/api/user/me` | Bearer | Profile fields incl. `latitude`/`longitude`, `pronouns`, `mediums`, banner/privacy settings |
| PATCH | `{{baseUrl}}/api/user/me/password` | Bearer | `{ "currentPassword", "newPassword" }` |
| PATCH | `{{baseUrl}}/api/user/me/notification-preferences` | Bearer | `{ "push"?, "dailyDigest"? }` |
| PATCH | `{{baseUrl}}/api/user/me/username` \| `/role` | Bearer | — |
| GET | `{{baseUrl}}/api/user/:username` | Optional Bearer | — |
| GET | `{{baseUrl}}/api/users/blocked` | Bearer | — |
| POST/DELETE | `{{baseUrl}}/api/users/:username/block` | Bearer | — |
| GET | `{{baseUrl}}/api/users/nearby` | Optional Bearer | Query: `lat`, `lng`, `radiusKm?`, `limit?` |
| POST | `{{baseUrl}}/api/user/me/onboarding/*` | Bearer | See above |
| POST/GET | `{{baseUrl}}/api/user/me/seller/*` | Bearer | See above (disable is state-checked, see above) |
| GET | `{{baseUrl}}/api/user/me/saved/pieces` \| `/saved/posts` \| `/orders` \| `/sales` \| `/series` | Bearer | — |
| GET/POST/PATCH/DELETE | `{{baseUrl}}/api/user/me/addresses/*` | Bearer | See above |
| POST/DELETE | `{{baseUrl}}/api/user/me/devices` | Bearer | `{ "platform", "pushToken" }` |
| POST | `{{baseUrl}}/api/media/presign` | Bearer | `{ "purpose", "contentType" }` |
| POST/PATCH/GET/DELETE | `{{baseUrl}}/api/pieces/*` | Varies | See above |
| GET/POST | `{{baseUrl}}/api/pieces/:id/{comments,shipping-quote,collect,related-posts}` | Varies | See above |
| POST/PATCH/GET/DELETE | `{{baseUrl}}/api/posts/*` | Varies | See above |
| GET | `{{baseUrl}}/api/posts/:id/comments` | — | Query: `cursor?`, `limit?` |
| GET | `{{baseUrl}}/api/users/:username/{pieces,posts,series,followers,following}` | Optional Bearer | Private accounts gate grids |
| POST/DELETE | `{{baseUrl}}/api/users/:username/follow` | Bearer | — |
| POST/DELETE | `{{baseUrl}}/api/pieces/:id/{like,save}` \| `/api/posts/:id/{like,save}` | Bearer | — |
| GET | `{{baseUrl}}/api/feed/following` | Bearer | Query: `cursor?`, `limit?` |
| GET | `{{baseUrl}}/api/feed/explore` | Optional Bearer | Query: `medium?`, `cursor?`, `limit?` |
| GET | `{{baseUrl}}/api/feed/for-you` | Bearer | Query: `cursor?`, `limit?` (stub) |
| POST/PATCH/GET/DELETE | `{{baseUrl}}/api/series/*` | Varies | See above |
| GET/PATCH/POST | `{{baseUrl}}/api/notifications/*` | Bearer | See above |
| GET/POST/PATCH | `{{baseUrl}}/api/conversations/*` | Bearer | See Conversations |
| GET/POST/PATCH/DELETE | `{{baseUrl}}/api/collections/*` | Bearer | See Collections |
| GET/PATCH/POST | `{{baseUrl}}/api/orders/*` | Bearer | See above |

**Auth column:** "Bearer" = `Authorization: Bearer {{accessToken}}` required; "Optional Bearer" = works without a token but returns viewer-specific fields (`isLiked`/`isSaved`/`isFollowing`) when one is sent; "Cookie" = sent automatically by Postman after login/register/refresh.
