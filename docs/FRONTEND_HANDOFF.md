# Frontend handoff

What changed on the backend, and what the app needs to do about it. Each backend phase adds a section here as it lands.

Read this against the app on `origin/main`. "No app change" means the current app code already works with the new backend.

Responses keep the usual envelope `{success, message, data}`. Errors are `{success: false, message}` with the HTTP status shown.

| Phase | Status | App work needed |
|---|---|---|
| 1. Security | Done, on `development` (`2b46cbd`) | Small: error messages on sign-in and code screens |
| 2. Refunds and cancellation | Done, on `development` (`2b46cbd`) | Order detail: cancel buttons, artist cancel confirmation, buyer "Report an issue" |
| 2A. Quick fixes | Done, on `development` (`f0e5c8a`) | Use the piece on order rows; logout-all message; founding-rate end date |
| 3. Ticketing core | Done, on `development` (`25460a1`) | Host ticket-type screens; free tickets through `/orders`; checkout polling; tickets screen already works |
| 3A. Google Wallet and Apple Wallet | Done, on `development` (`25af41f`) | "Add to Google Wallet" / "Add to Apple Wallet" buttons on the confirmation and ticket screens |
| 4. Ticket refunds, waitlist, check-in, host cancel and reschedule, host payouts | Done, on `development` (`5063d2f`) | Refund button; waitlist screens already built; scanner and guest list; reschedule and cancel-preview screens |
| 5. Co-host and artist invites, piece approvals, bidding end | Done, on `development` (`5063d2f`) | Invite inbox and accept/decline; piece request screen; "Waiting for" in Lineup; bidding end picker; Exhibited on piece pages |
| 6. Account and settings (going public, selling off, deletion check, hide from collection) | Done, on `development` (`5063d2f`) | Drop the one-by-one approvals; new turn-off message; Delete account reads the check; Hide from my Collection; fees from seller-terms |
| 7a. Auctions v2 (charge at close, 12-hour fix window, second-chance offer) | Done, on `development` (`a655a20`) | Card check and Ship to at the first bid; new increments; no extensions; payment-failed and second-chance screens |
| 7b. Shipping, insurance and tax in the charge at close | Done (not yet committed); works once Convelio is on | Show the automatic charge; keep the auction checkout only as the fallback |
| 8. Convelio shipping and checkout pricing | Done, on `development` (`5fea30f`); Convelio is off until switched on | New checkout (speed, delivery type, insurance, live quote, pending quote); listing fields; pickup address screen; shipment tracker; piece-page estimate |

The shared dev database (Aiven) is at migration `060_convelio_shipping`, so a build pointed at it can use everything through Phase 8.

---

## Phase 1: Security

**Shapes and endpoints:** no endpoint or response shape changed. Three behaviours did.

### 1.1 Sign-in, sign-up and code screens: new limits

| Endpoint | New behaviour | Status and message |
|---|---|---|
| `POST /api/auth/login` | At most 20 attempts a minute from one network address, on top of the existing per-account lock | `429` "Too many requests. Please try again later." |
| `POST /api/auth/otp/verify` (all purposes, **now including sign-up**) | At most 20 checks a minute from one address | `429` (same message) |
| `POST /api/auth/otp/verify`, `POST /api/auth/register`, `POST /api/user/me/email/confirm-change` | A code is thrown away after **5 wrong guesses** (before, this applied only to login and reset codes). One email can make at most 30 wrong guesses an hour across all its codes | `400`, the same "verification failed" message as a wrong code |
| `login`, `register`, `otp/verify` | If the server's Redis is down, these refuse instead of running without limits | `503` "Sign-in is temporarily unavailable. Please try again shortly." |

**App changes:**
- [ ] Show the `message` from 429 and 503 responses on the login, sign-up and code screens. Don't show a generic "wrong code" or "wrong password" for them.
- [ ] After 5 wrong code entries, the code no longer works even if the user then types it correctly. After the fifth failure, show "Request a new code" (call the existing resend). The server returns the same 400 for a wrong code and a dead code, so the app has to count.

### 1.2 Media uploads in local development only

Production and staging use S3 and are unchanged.

When a developer runs the backend without S3, the `presignedPutUrl` from `POST /api/media/presign` now carries a signature in its query string (`?ct=…&max=…&exp=…&sig=…`), the same way an S3 URL does. The upload must:
- go to the URL exactly as given, with the query string;
- send a `Content-Type` header equal to the requested `contentType`;
- send at most `contentLength` bytes (or the 20 MB image / 100 MB video limit), within an hour.

**No app change:** `ApiClient.uploadToPresignedUrl` already does all of this.

Local media now only serves images and video.

### 1.3 Staging needs its own S3 bucket

Staging (Render) no longer falls back to saving uploads on the server's disk. It needs `S3_BUCKET`, `S3_PUBLIC_BASE_URL` and the AWS keys, or it won't start. This affects testing builds pointed at staging, not the app code.

### 1.4 Server-side only (for awareness)

- Rate limits now use the real client address: the proxy's `X-Forwarded-For`, trusted for `TRUSTED_PROXY_HOPS` entries. Before, a client could fake it.
- Session records now store the client's real IP, not the proxy's.
- Email codes are generated with a secure random source.
- Deploys no longer copy credentials, specs, Postman collections or tests to the server.

---

## Phase 2: Refunds and cancelling orders

**Rule decided 9 Nov:** once an art order is paid, only the **artist** (or a Studio 3 admin) can cancel it, and the buyer is refunded in full automatically. The buyer can never cancel a paid order. Their route is **Report an issue**.

### 2.1 `PATCH /api/orders/:id` with `{"status": "cancelled"}`

Same endpoint and body as today. The artist may also send an optional `"reason"`, kept with the refund.

| Order status | Who | Result |
|---|---|---|
| `pending_payment` | Buyer or artist | `200`, order `status: "cancelled"`. The server cancels the Stripe payment first |
| `pending_payment`, payment already going through | Buyer or artist | `409` "This payment is already going through. Check back in a moment." The order stays as it is; reload it shortly (it will become `paid`) |
| `paid` | **Artist** | `200`, order **`status: "refunded"`** (not `cancelled`). The buyer is refunded in full and notified "Order cancelled" |
| `paid` | **Buyer** | `403` "A paid order can't be cancelled by the buyer. If something's wrong, report an issue and we'll sort it out." |
| `paid`, refund failed at Stripe | Artist | `502` with a message. The order stays `paid`. Show the message; Studio 3 retries from the admin console |
| `shipped` or later | Anyone | `409` "Cannot cancel an order in status 'shipped'." |

Any other `status` value is still refused with `403` (this was already the case).

**App changes in `lib/screens/order_detail_page.dart`:**
- [ ] **Buyer** (`_buyerActions`): show "Cancel order" **only** for `pending_payment`. For `paid`, `shipped` and `awaiting_confirmation`, show **"Report an issue"** instead, using the existing `OrderService.reportIssue`.
- [ ] **Artist** (`_sellerActions`): keep "Cancel order" for `pending_payment` and `paid`. For `paid`, show a confirmation first: "Cancel this sale? The buyer will be refunded in full and the piece goes back on sale."
- [ ] **Artist:** remove "Mark as shipped" and "Mark as completed". The backend already refuses both with `403`; shipping is updated by Studio 3 (and later by Convelio), and completion comes from the buyer's "Confirm received".
- [ ] Treat a `refunded` status like `cancelled` in the order list and detail, labelled "Refunded".

### 2.2 Notifications

Inbox type `order`, `targetType: "order"`, `targetId` = the order ID. A tap should open the order.

| When | Who | Title |
|---|---|---|
| The artist cancels a paid order | Buyer | "Order cancelled" |
| An admin refunds an order | Buyer | "Refund issued" |

### 2.3 Server-side only (for awareness)

- **One refund path:** every refund (admin, artist cancel, late payment) goes through one service. It holds the artist's payout before the money is returned, so a refund and a payout can no longer both go out.
- **Late payments:** a payment that completes after its order was cancelled is now refunded automatically. Before, the buyer was charged for nothing.
- **Shipped orders:** refunding a `shipped` or `awaiting_confirmation` order (for example, lost in transit) now works end to end. Before, Stripe refunded the buyer but the order stayed "shipped".
- **Ledger fix (BE-1):** refunds now record that Stripe keeps its fee. Earlier refunds are corrected with `scripts/fix_be1_refund_ledger.py` (dry run first).

---

## Phase 2A: Quick fixes

### 2A.1 Order rows carry their piece (B34)

`GET /api/user/me/orders`, `GET /api/user/me/sales` and `GET /api/orders/:id`: each entry in `items` now has a `piece` object. Each order also has `fromAuction`.

```json
{
  "items": [{
    "pieceId": "…", "priceCents": 50000, "quantity": 1,
    "piece": {"id": "…", "title": "Paper Planes III", "mediaUrl": "https://…", "listingType": "fixed"}
  }],
  "fromAuction": false
}
```

- `piece.mediaUrl` is always a picture. For a piece with a video cover it's the piece's first photo, the same rule the notifications use. It's `null` if the piece has no photo.
- `piece` is `null` if the piece no longer exists.
- `fromAuction` is `true` for an auction win, so the row can say AUCTION instead of SOLD.

**App changes:**
- [ ] Sales and Purchases rows: take the title and picture from `items[0].piece`, and stop fetching each piece.
- [ ] Show AUCTION when `fromAuction` is true.

### 2A.2 Log out of all devices keeps this phone (B34)

`POST /api/auth/logout-all` now signs out every **other** device and keeps the one that asked.
- The response no longer clears this device's refresh cookie.
- The message is "Logged out of all other devices."

**App changes:**
- [ ] After the call succeeds, stay signed in and show the message. Don't send the user to the login screen.

### 2A.3 Founding-artist rate end date (B3)

`GET /api/user/me/commission-quote` adds `reducedRateUntil`: an ISO date-time when the reduced rate ends, or `null` when it has no end date or there's no reduced rate.
- After that date, `reducedRate` is `false` and the standard rate applies.
- Orders and auctions keep the rate they were sold at.

**App changes:**
- [ ] Optional: when `reducedRateUntil` is set, add "until <Month Year>" to the "15% founding artist commission" line.

### 2A.4 Auction fixes (no app change needed)

- **"Update your card" notification:** `bid_hold_expired` now has `targetType: "piece"` and `targetId` set to the piece, so a tap opens the auction. Before, it opened nothing.
- **Bidding again after a card hold expired:** this no longer leaves two active bids. The earlier one becomes `outbid`.
- **Cancelling an auction that hasn't opened** (draft or scheduled) with `POST /api/pieces/:id/auction/cancel` now actually cancels it. Before, it answered 200 and left it in place.
- **`docs/API.md`** now lists every status relist accepts: `needs_seller_action`, `closed_no_bids` and `closed_reserve_not_met`.

---

## Phase 3: Ticketing core (B22, plus age limit and visibility from B28)

Built to the ticketing spec of 25 Oct 2026. The endpoints the app already calls on `origin/main` (`/orders/quote`, `/orders`, `/user/me/tickets`, the event detail fields) keep exactly the shapes `lib/models/event_order.dart` and `studio_event.dart` read. Most of the buyer side works with no app change.

### 3.1 New fields on events

**Event card** (every list), added:
- `isFree`: now real. `false` when any open ticket type costs money.
- `ticketCount`: how many tickets the viewer holds.
- `ageLimit`: `"all"`, `"18"` or `"21"`.

**Event detail** (`GET /api/events/:id`), added:

| Field | Type | Notes |
|---|---|---|
| `ticketTypes` | array | See 3.2. Empty for an event with no ticket types (free RSVP event) |
| `refundPolicy` | `"7d"` / `"24h"` / `"none"` | |
| `refundPolicyText` | string | e.g. "Refunds up to 7 days before the event." |
| `visibility` | `"public"` / `"link"` | Link-only events never appear in browse, search, feeds, Explore or profiles, but open from their link |

`waitlist` arrives in Phase 4 (see 4.2).

**Create and edit** (`POST /api/events`, `PATCH /api/events/:id`) accept:
- `refundPolicy`, `ageLimit` and `visibility`, with the values above. Anything else returns 400.
- `refundPolicy` can't change once a paid ticket has sold: 409 "Paid tickets have sold under the current refund policy, so it can't change."

### 3.2 Ticket types

Each entry in `ticketTypes`:

```json
{"id": "…", "name": "VIP", "priceCents": 2500, "includes": "Drinks",
 "left": 7, "perOrderLimit": 4, "saleStartsAt": null, "saleEndsAt": null,
 "onSale": true, "soldOut": false}
```

- `left` is `null` for no cap, and `0` when sold out. Show "Only N left" in red when it's 5 or fewer (spec).
- The host's own view also gets `quantity`, `sold`, `hostNetCents` (what they receive per ticket, e.g. a $25 ticket = 2300) and `closed`.

**Host endpoints** (host only, otherwise 404):

| Method | Path | Body | Notes |
|---|---|---|---|
| GET | `/api/events/:id/ticket-types` | — | Also open to anyone for a published event |
| POST | `/api/events/:id/ticket-types` | `{name, priceCents, includes?, quantity?, perOrderLimit?, saleStartsAt?, saleEndsAt?, sortOrder?}` | `201` with `{id, ticketTypes}`. **Paid types need a seller profile:** otherwise `403` "Set up your seller profile to sell paid tickets…", and the app should open seller setup. The minimum paid price is $0.50 |
| PATCH | `/api/events/:id/ticket-types/:typeId` | any of the above | A type that has sold can't change price (409), and its quantity can't go below what's sold (409) |
| DELETE | `/api/events/:id/ticket-types/:typeId` | — | Deletes it if nobody bought it; otherwise **closes** it (`result: "closed"`), and its tickets stay valid |

**App changes:**
- [ ] Hosting, Tickets step: replace the "Free RSVP with a cap" screen with ticket types: name, price or free, what's included, quantity, max per order, sale start and end.
- [ ] Show "You receive $X per ticket" from `hostNetCents`.
- [ ] Add pickers for refund policy, age limit (Details), and public or link-only (Details).

### 3.3 Buying

**Quote:** `POST /api/events/:id/orders/quote` with `{"items": [{"ticketTypeId", "quantity"}]}` returns:

```json
{"subtotalCents": 5000, "serviceFeeCents": 0, "taxCents": 413, "totalCents": 5413,
 "refundPolicy": "Refunds up to 7 days before the event.", "items": [...]}
```

**Buyers never pay a fee:** `serviceFeeCents` is always `0` (spec).

**Order:** `POST /api/events/:id/orders` with the same `items`, plus an optional `email` (where the tickets are sent; the account email by default).

| Case | Response |
|---|---|
| Paid | `201` `{id, orderId, clientSecret, status: "pending_payment", totalCents, holdExpiresAt}`. The tickets are **held for 10 minutes**. Present the PaymentSheet with `clientSecret` |
| Free only | `201` with `clientSecret: null` and `status: "paid"`: the tickets are issued already |
| Sold out, over the per-order limit, sale not open, or event over | `409` with a message to show ("Only 2 VIP left.", "VIP is sold out.") |
| Host buying their own tickets | `400` "You're hosting this one." |

After the PaymentSheet succeeds, the tickets are issued by the server when Stripe confirms. This usually takes a second or two.

**Poll:** `GET /api/events/:id/orders/:orderId` returns `{status, ticketCount, …}` until `status` is `"paid"`. If the 10 minutes run out before payment, `status` becomes `"expired"` and the tickets go back on sale.

**My tickets:** `GET /api/user/me/tickets?eventId=` returns `{tickets: [{id, eventId, orderId, code, ticketTypeName, number, status, scannedAt}]}`.
- `code` is the value to draw as the QR. It's signed, so a made-up code won't pass the door.
- `number` is a short code like `S3-T-8KQ2XM`, for typing at the door.

**App changes:**
- [ ] `EventCheckoutPage`: after `presentPaymentSheet`, poll `GET …/orders/:orderId` (about every 1 second, for up to 20 seconds) and open the confirmation once it's `paid`, instead of only reloading the event.
- [ ] Optionally show a countdown from `holdExpiresAt`.
- [ ] **Free tickets:** when the choice is free, call `POST /api/events/:id/orders` with the chosen `items`, not `setRsvp`. RSVP still works (3.4), but it can't say how many or which type, so a choice of 2 free tickets only gets 1 through RSVP.
- [x] `EventTicketPage`, `my_events_page.dart` (`ticketCount`) and `event_order.dart` already read the right fields.

### 3.4 The free one-tap RSVP

`POST /api/events/:id/rsvp {going}` keeps its request and response, so installed apps keep working. Behind it:

| Event | `going: true` | `going: false` |
|---|---|---|
| No ticket types | Issues one **General Admission** ticket with a QR code, capped by the event's `capacity` as before (409 when full) | Voids the free ticket |
| Has a free ticket type | Issues one ticket of the first free type with stock left | Voids the viewer's free tickets |
| Paid types only | `409` "This event needs a ticket. Choose tickets to continue." | — |
| Viewer holds paid tickets | — | Free tickets are voided, then `409` "Your paid tickets are still valid…". Paid tickets are refunded from the ticket (4.1) |

Buying any ticket marks the buyer as going. `viewerIsGoing`, `rsvpCount`, `scope=going`, the guest list and the event reminders all include ticket holders.

### 3.5 Emails

The server sends the tickets and the receipt by email (spec: "the app, not Stripe"): the event, the local time with its zone, each ticket's type and number, and the receipt for paid orders. The QR code itself is shown in the app, and from Phase 3A in Google and Apple Wallet.

### 3.6 Server-side only (for awareness)

- **Stock is exact under concurrency:** the ticket type's row is locked while counting held and issued tickets. A test proves two buyers can't both get the last ticket.
- **The 10-minute hold** is released by a job that runs every minute (`events.expire_ticket_holds`) once Stripe agrees nothing was paid. A payment that arrives after its hold gets the tickets if they're still available, and is otherwise refunded automatically.
- **Ledger:** the host's share (price minus the 8% ticket fee, snapshotted per order) is owed to the host and paid out after the event (Phase 4).
- **Tax:** sales tax is now whole-cent arithmetic for art and tickets (`SALES_TAX_RATE_BPS`, default 8.25%).
- **QR codes:** signed with Ed25519 (`TICKET_SIGNING_KEY`). In Phase 4, door scanners will check them offline with only the public key.
- **Deploy:** migration `054_event_ticketing`, and Celery beat must be restarted to pick up the new job.

---

## Phase 3A: Google Wallet and Apple Wallet (B25)

The client's requirement: after booking, the buyer can put their tickets in **Google Wallet**, and **Apple Wallet** on iPhone. The pass shows the same QR code as the app's ticket screen, and the door scanner checks it the same way, so a ticket is still one admission whichever app shows it.

### 3A.1 Is a wallet available?

`GET /api/user/me/tickets` (each ticket) and `GET /api/events/:id/orders/:orderId` now carry:

```json
"walletPassAvailable": true,
"walletPasses": {"apple": true, "google": true}
```

- `walletPassAvailable` is B25's single flag: true if either wallet works.
- `walletPasses` says which.
- A wallet is `false` until its credentials are set on the server (see `docs/MANUAL_SETUP.md` 5.6 and 6.6). **Only show a button when its flag is true.**

### 3A.2 Getting the pass

`GET /api/events/:id/orders/:orderId/wallet-pass` (buyer only; anyone else gets 404):

| Query | Response |
|---|---|
| `?platform=google` | `200` `{"data": {"saveUrl": "https://pay.google.com/gp/v/save/…"}}`. Open this URL (it shows Google's own "Save to Google Wallet" screen). One link holds every ticket in the order |
| none (Apple) | `200` raw bytes. With one ticket it's `application/vnd.apple.pkpass`; with several it's `application/vnd.apple.pkpasses` (a bundle, iOS 15+) |
| Apple, `&ticketId=<id>` | Just that ticket's `.pkpass` |
| Order refunded or cancelled, or no valid tickets left | `410` "These tickets are no longer valid." |
| Not paid yet | `409` "The tickets haven't been issued yet." Wait for `status: "paid"` (3.3) |
| That wallet isn't set up on the server | `503` |

**App changes:**
- [ ] `EventConfirmedPage` and `EventTicketPage`: show **"Add to Apple Wallet"** on iOS when `walletPasses.apple`, and **"Add to Google Wallet"** on Android when `walletPasses.google`. Use the official badge artwork from Apple and Google.
- [ ] **Google:** call `…/wallet-pass?platform=google`, then open `saveUrl` with `url_launcher` in an external browser or app.
- [ ] **Apple:** download `…/wallet-pass` (with the bearer token) and hand the bytes to PassKit. A Flutter package such as `flutter_wallet_card` or `pass_flutter` can present the "Add" sheet from bytes, or save the file and open it.

### 3A.3 Passes stay up to date

- **Voided or refunded ticket:** the pass becomes **void** in Apple Wallet and **inactive** in Google Wallet within a few seconds. This happens on "not going", a refund (4.1) and a cancelled event (4.5).
- **Moved event:** passes show the new date (4.4).

No app change is needed for this; Apple and Google handle it.

### 3A.4 The email

The ticket confirmation email now has "Add to Apple Wallet" and "Add to Google Wallet" buttons when they're set up. The Apple link works without logging in (it's signed and lasts 30 days), so buyers can add tickets from a computer or another device.

### 3A.5 Server-side only (for awareness)

- **Signing:**
    - Apple passes are signed with the Pass Type ID certificate plus Apple's WWDR certificate (PKCS#7). The tests check the signature with OpenSSL, the way Apple does.
    - Google links are JWTs signed with the Wallet service account.
- **Apple's pass web service** lives under `/api/wallet/apple/v1/` (register, unregister, what changed, latest pass, log). Updates are pushed through Apple's push service with the pass certificate, after the change is committed.
- **Configuration:** new settings, all optional:
    - `GOOGLE_WALLET_ISSUER_ID`, `GOOGLE_WALLET_SERVICE_ACCOUNT_JSON`
    - `APPLE_PASS_TYPE_ID`, `APPLE_TEAM_ID`, `APPLE_PASS_CERT_P12`, `APPLE_PASS_CERT_PASSWORD`, `APPLE_WWDR_CERT`
- **`BACKEND_URL` must be `https://`** for Apple pass updates and the Google pass logo.
- **Deploy:** migration `055_wallet_passes`. `deploy/deploy.sh` carries the wallet settings, the tax rate and `TICKET_SIGNING_KEY` into production. It generates the signing key on the first deploy and keeps it from then on.
- **Until the client's Google and Apple credentials are in,** both flags in 3A.1 are `false` on staging and production, so test the buttons against a local server with test credentials, or wait for those.

---

## Phase 4: Refunds, waitlist, check-in, reschedule, cancel, host payouts (B22, B26, B28)

Built to the ticketing spec of 25 Oct 2026. The waitlist uses the exact calls the app already makes (`lib/services/event_service.dart`, `EventWaitlist` in `studio_event.dart`), so those screens work as they are.

### 4.1 Refunds by the buyer

**Window (spec):** a refund is allowed only inside the event's refund window:

| `refundPolicy` | Refunds close |
|---|---|
| `7d` | 7 days before the start |
| `24h` | 24 hours before the start |
| `none` | Never open |

After a **reschedule**, anyone can get a full refund until the new start, whatever the policy said.

**New fields:**
- Each ticket in `GET /api/user/me/tickets` has `refundable: bool`. It's true only for a paid ticket that hasn't been checked in, inside the window. **Show the refund button only when it's true** (spec: "Outside the window: no refund button").
- `GET /api/events/:id/orders/:orderId` adds `refundable`, `refundableUntil` (ISO or `null`) and `refundedCents`. Order `status` can now also be `partly_refunded` or `refunded`.
- The event detail adds `refundableUntil`.

**Endpoint:** `POST /api/events/:id/orders/:orderId/refund`, buyer only.

| Body | Result |
|---|---|
| `{}` | Refunds every unused ticket in the order |
| `{"ticketIds": ["…"]}` | Refunds just those tickets |

`200` returns the order (as `GET …/orders/:orderId`) plus `refund: {status, amountCents, ticketIds}`. The amount is the tickets' price plus their tax; the last refund of an order returns exactly what's left.

| Case | Status and message |
|---|---|
| Outside the window, or `none` | `409` "Refunds for this event have closed." |
| A checked-in ticket | `409` "A ticket that's been checked in can't be refunded." |
| Free tickets | `409`: use "Not going" (RSVP) instead |
| Already going through | `409` |
| Stripe failed | `502` with a message. Studio 3 retries it; the tickets stay valid until the money is back |
| Someone else's order | `404` |

After a refund, the tickets disappear from `GET /api/user/me/tickets`. Their QR codes no longer scan, the wallet pass turns void, and the tickets go to the waitlist first (4.2). When no tickets are left, the buyer is no longer `viewerIsGoing`.

Notification: type `ticket_refunded`, target the event: "We've refunded $21.65 for your tickets to …".

**App changes:**
- [ ] `EventTicketPage`: show "Refund" when `refundable`, with a confirmation that names the amount from the quote (price plus tax). Use `ticketIds` to refund one ticket of several.
- [ ] Show "Refunds closed" (or nothing) when it's false.

### 4.2 Waitlist (`EvWaitlist*`, already built in the app)

On the event detail, and on event cards in Your events when the viewer is in line:

```json
"waitlist": {
  "open": true,
  "wanted": [{"ticketTypeId": "…", "quantity": 2}],
  "held": {"items": [{"ticketTypeId": "…", "quantity": 1}], "until": "2026-11-21T03:40:00+00:00"},
  "waitingCount": 3,
  "waitingByType": {"<ticketTypeId>": 3}
}
```

- `open` is true while every ticket type on sale is sold out and the event hasn't started.
- `held` is `null` when nothing is held.
- `waitingCount` and `waitingByType` are for the host only.
- `waitlist` is left out when there's nothing to show (B22).
- The host's ticket types also carry `waiting`: how many people want that type.

**Endpoints** (exactly the app's):

| Method | Path | Body | Result |
|---|---|---|---|
| POST | `/api/events/:id/waitlist` | `{"items": [{ticketTypeId, quantity}]}` | `200` with `waitlist`. Changing what you want keeps your place |
| DELETE | `/api/events/:id/waitlist` | — | `200` `{left: true}`. Anything held passes to the next person |
| POST | `/api/events/:id/waitlist/hold/release` | — | `200` with `waitlist`: "Keep waiting for [other type]". The held type is dropped from `wanted`; the others keep their place |

Joining is refused with `409` when a chosen type isn't sold out ("VIP isn't sold out. Choose tickets instead."), over its per-order limit, or the event has started.

**Rules (spec):**
- Places are first come, first served.
- A freed ticket (from a refund, an unpaid checkout running out, someone leaving, or the host adding more) is held for the next person who asked for that type, up to the number they asked for. Any extra goes to the people after them at the same time. Only what the whole line doesn't want goes on sale.
- **How long a hold lasts** depends on how soon the event starts:

  | Event starts in | Hold lasts |
  |---|---|
  | More than 24 hours | 2 hours |
  | 2 to 24 hours | 30 minutes |
  | Less than 2 hours | 10 minutes |

- While held, the person buys through the normal `POST …/orders`, or taps Reserve (`POST …/rsvp`) for a free type. Nobody else can buy those tickets (`409`).
- A hold that runs out passes on, and that person is taken off the waitlist.
- The waitlist closes quietly when the event starts or is cancelled.
- An event with no ticket types (a plain capacity RSVP) has no waitlist. Add a free ticket type to get one.

**Notifications** (target the event):

| Type | When | Message |
|---|---|---|
| `waitlist_hold` | Tickets are held for them. Also sent by email | "2 tickets are held for you until 9:40 PM CST." `payload.heldUntil` is the ISO time |
| `waitlist_hold_ended` | Their hold ran out | — |
| `event_tickets_available` (B26) | A sold-out event has tickets on sale again. Sent to people who saved it and aren't going, once per reopening. Not sent when it reopens within 10 minutes of selling out, which is just a checkout running out | — |

**App changes:**
- [ ] A tap on `waitlist_hold` opens Choose tickets with `held.items` already picked and a countdown to `held.until`. This is already built; check the deep link.
- [ ] Handle the `event_tickets_available` and `waitlist_hold_ended` notification types: open the event.

### 4.3 Check-in at the door (`HostScanner`)

Only the host and co-hosts can use these; anyone else gets `404`. Phase 5 limits co-hosts to those who accepted their invite.

| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/api/events/:id/tickets/:code/check-in` | optional `{"scannedAt": ISO}` | `:code` is the QR value, the ticket number (`S3-T-8KQ2XM`, any case) or the ticket id (for the guest list's check-in by hand) |
| POST | `/api/events/:id/check-in/sync` | `{"scans": [{"code", "scannedAt"}]}` | Offline scans, up to 500. Returns `results` in the same order |
| GET | `/api/events/:id/guests?q=` | — | Guest list, searchable by name, username or ticket number |
| GET | `/api/events/:id/check-in/manifest` | — | For offline use (below) |

A scan always answers `200` with a `result` to show:

```json
{"result": "checked_in", "message": "Checked in: Ana Ruiz, VIP (1 of 2).",
 "ticket": {"id", "number", "ticketTypeName", "holderName", "holderUsername", "holderAvatarUrl",
            "position": 1, "of": 2, "status", "checkedInAt"},
 "checkedIn": 48, "total": 62}
```

| `result` | `message` |
|---|---|
| `checked_in` | "Checked in: [name], [type] (1 of 2)." |
| `already_checked_in` | "Already checked in at 8:14 PM CST." |
| `invalid` | "Not a valid ticket." / "…it's for a different event." / "…it was refunded or cancelled." |

- **Offline:** store each scan with its time and send them to `…/check-in/sync` when back online. The earliest scan of a ticket wins. If another device's earlier scan arrives later, this device's scan reads `already_checked_in` from then on.
- **Manifest:** `GET …/check-in/manifest` returns:
  - `publicKey` (Ed25519, base64) and `codePrefix` (`S3T1.`), so the app can check a QR's signature without the network;
  - every ticket, with its state;
  - `ageLimit` and the counts.
- **Guest list:** returns `{guests: [...], checkedIn, total, ageLimit}`. Show the age limit as a reminder (spec).

**App changes:**
- [ ] Manage event: replace "Guest list" with "Check in guests" (B28).
- [ ] The scanner shows the live count, the age limit and each scan result. It also needs Enter code and a guest list with search and check-in by hand.
- [ ] Offline: download the manifest when the scanner opens, queue scans with their time, and sync them.

### 4.4 Reschedule (`HostReschedule`)

`POST /api/events/:id/reschedule` with `{"startsAt", "endsAt", "message"?}` (host only).

- `200` returns the event, with `rescheduledAt`, `rescheduleMessage`, the new times and `refundableUntil` (the new start).
- Every ticket holder is told once:
  - notification `event_rescheduled` ("… has moved to Sat, Nov 28 · 8:00 PM CST. Your tickets still work.");
  - an email with the host's message.
- Tickets keep working, and refunds are open until the new start (4.1).
- Wallet passes show the new date.
- The day-before reminder goes out again for the new date.
- An auction on the event moves with it. **If bidding has started, the reschedule is refused** (`409`).
- `400` when the new start is in the past, the event is too short for its auction, or nothing changed.
- `PATCH /api/events/:id` with new dates on a published event still answers `409`, now saying to use Reschedule.

Event cards carry `rescheduledAt` too.

**App changes:**
- [ ] The Reschedule screen: new date and time, and an optional message.
- [ ] Show "Rescheduled" with the message on the event page and the ticket.

### 4.5 Host cancels

**Preview first:** `GET /api/events/:id/cancel-preview`:

```json
{"ticketsToVoid": 62, "ordersToRefund": 40, "refundTotalCents": 154300,
 "cardFeesCents": 4810, "waitlistCount": 7, "guestsToNotify": 41}
```

Show the host the tickets, the refund total and **the card fees they pay** (spec).

**Confirm:** `POST /api/events/:id/cancel` with `{"message"?: "…"}`. The existing `reason` field still works.

The response adds:
- `ticketsVoided`
- `ordersToRefund`
- `refundTotalCents`
- `cardFeesCents`
- `waitlistCleared`

On confirm:
- every ticket stops working at once, and wallet passes turn void;
- every paid order is refunded in full, in the background, within a minute;
- unpaid checkouts are called off, and a payment that slips through anyway is refunded;
- the waitlist is cleared;
- tagged pieces come off sale (as before);
- guests and everyone on the waitlist get the `event_cancelled` notification and an email with the message.

**Deleting:** `DELETE /api/events/:id` on an event that has any tickets is refused with `409` "…Cancel it instead". Drafts and events with no tickets delete as before.

**App changes:**
- [ ] The cancel flow calls the preview, shows its numbers, and takes an optional message.

### 4.6 Hosts get paid after the event (server-side, for awareness)

- **Archiving:** an event moves to `archived` once it ends (hourly).
- **Payout:** a day after it ends, the host's ticket money goes to their Stripe account in one transfer. That is the price less the 8% fee, less refunds, less any card fees they still owe from an event they cancelled.
- **Failures:** a payout that fails (for example, the host's Stripe isn't set up) is retried hourly for four days, then listed for Studio 3 in the admin console.
- **No app change needed.** The host sees the money in their Stripe Express dashboard.

### 4.7 Server-side only (for awareness)

- **Money:**
  - refunds use the same three steps as art refunds: record the refund, then call Stripe with no lock held, then mark the tickets;
  - a part refund and the final one together return exactly the order total;
  - the ledger stays balanced.
- **Concurrency:** two doors scanning one code at once let it in once (there's a test for it). Stock counts waitlist holds the same way as checkout holds.
- **Admin console (Needs attention):**
  - ticket refunds that failed, with Retry;
  - host payouts that failed, with Retry;
  - card fees hosts owe.

  This also fixes an earlier bug where the failed-refunds panel was pasted into the page title.
- **Jobs:** `events.expire_waitlist_offers` now runs every minute and does the real work. `events.archive_past` (hourly) archives events and pays hosts.
- **Deploy:**
  - migration `056_ticketing_phase4`;
  - new optional setting `EVENT_PAYOUT_DELAY_HOURS` (default 24);
  - restart Celery beat to pick up the new schedule.

---

## Phase 5: Invites, piece approval, bidding end, Exhibited, time zones (B26, B28)

Built to the ticketing spec's "Lineup", "Art at events", "Notifications" and "Time".

### 5.1 Co-host and featured-artist invites (`ArtistInvite`)

`PUT /api/events/:id/people` keeps its request. What changed: **new people are invited**, and they appear on the event only once they accept. Everyone already on a bill before this release stays as they were (accepted).

Event detail:
- `cohosts` and `artists` list **accepted people only**.
- `invites`: `[{username, name, avatarUrl, role, status}]` with `status` `invited`, `accepted` or `declined`. Only the host and accepted co-hosts get it. This is the host's Invited / Accepted view.
- `viewerInvite`: `{roles, status}` when the viewer is on the bill.
- `viewerCanEdit`: true for the host and accepted co-hosts.

An invitee can open the event while it's still a draft, so they can see what they're saying yes to.

| Method | Path | Body | Result |
|---|---|---|---|
| POST | `/api/events/:id/invite/accept` | optional `{"role": "cohost"\|"artist"}` | `200` with the event plus `invite: {status, roles}`. `404` if no invite is waiting |
| POST | `/api/events/:id/invite/decline` | same | Same as accept |
| GET | `/api/user/me/event-invites` | — | `{invites: [{role, invitedAt, event: <card>}]}`: invites waiting for an answer |

Putting a declined person back on the list asks them again.

**Accepted co-hosts can:**
- edit the event (`PATCH`);
- manage ticket types;
- add and remove pieces;
- see the guest list and the QR codes;
- scan at the door.

**Only the host can:** publish, cancel, reschedule, delete, and change who is on the bill. A co-host calling any of these gets `404`.

**Notifications:**

| Type | Who | When | Message |
|---|---|---|---|
| `event_invite` | The invitee (also by email) | They're invited | "Ana invited you to be a co-host at Night Market." `payload.role` |
| `event_invite_response` | The host | The invitee answers | "Mira accepted your invite to be a featured artist." |

**App changes:**
- [ ] An invites list from `/me/event-invites`, plus the `event_invite` notification, each opening the event with Accept and Decline.
- [ ] In Lineup and Manage, show `invites` with their status.

### 5.2 Artist approval of tagged pieces (`ArtistPieceRequest`)

A host (or co-host) can now tag **anyone's** piece in any mode (`featured`, `sale` or `bid`), with a price. Before, selling someone else's work was a `403`.

- **Someone else's piece starts `waiting`.** Its own listing or auction is untouched until the artist approves, and it's hidden from the event page.
- **Pieces the host owns**, or the person adding owns, are approved straight away, as before.

Lineup entries now carry, for the host, co-hosts and the piece's artist:
- `approvalStatus` (`waiting`, `approved` or `declined`);
- `listingEnded` (whether approving ended the piece's own listing);
- `respondedAt`.

Everyone else sees approved pieces only.

| Method | Path | Body | Result |
|---|---|---|---|
| POST | `/api/events/:id/pieces/:pieceId/approve` | optional `{"priceCents"}` (the artist's own price) | `200` with the lineup entry. For `sale` or `bid`, this **ends the piece's own listing or auction** (bidders are told it was withdrawn). On a published event the piece is listed for the event at once |
| POST | `/api/events/:id/pieces/:pieceId/decline` | — | `200` |
| GET | `/api/user/me/piece-requests` | — | `{requests: [<lineup entry> + preview: {endsAuction, endsFixedListing, activeBidCount}, event: <card>]}` |

- Only the piece's artist can answer; anyone else gets `404`.
- A request already answered gets `409`.

**Rule (spec):** an event either sells pieces or auctions them, never both. Adding a `sale` piece to an event with `bid` pieces (or the other way round) gets `409` "An event either sells its pieces or auctions them, not both."

**Notifications:**

| Type | Who | Payload |
|---|---|---|
| `event_piece_request` | The artist (also by email) | `{pieceId, mode, priceCents, deliveryMode, endsAuction, endsFixedListing, activeBidCount}`, enough to show "approving ends your auction" |
| `event_piece_response` | The host | `{pieceId, approved}` |

**App changes:**
- [ ] The artist's request screen:
  - how the piece will be shown;
  - delivery;
  - an editable price;
  - the listing-conflict warning from `preview`;
  - Approve and Decline.
- [ ] Lineup shows "Waiting for [artist]" for `waiting` entries, and "Approved by [artist]" once approved.

### 5.3 When bidding ends

`POST /api/events` and `PATCH /api/events/:id` take `auctionEndsAt` (ISO, or `null` for the default). Event detail returns `auctionEndsAt`, the effective end. The default is 30 minutes before the event ends.

- **Limits:** it must be after the start and at least 30 minutes before the end, otherwise `400`.
- **Published event:** running auctions move to the new end, unless someone has bid (`409`).
- **Reschedule:** the bidding end moves with the event, keeping its distance from the end.

**App changes:**
- [ ] A "Bidding ends" picker in Details for auction events. Default it to 30 minutes before the end, and don't allow anything later.

### 5.4 Exhibited (B26)

When an event is over, every approved piece on its bill gets an Exhibited entry, once. This happens with the hourly archive. Piece detail (`GET /api/pieces/:id`) adds:

```json
"exhibitions": [{"eventId": "…", "title": "Night Market", "venueName": "Cedars Union",
                 "city": "Austin", "date": "2026-11-21T02:00:00+00:00", "month": "November 2026"}]
```

The list is newest first, and `[]` for none. The entry stays even if the event is later deleted.

**App changes:**
- [ ] Show "Exhibited" on the piece page: event name, venue and month (spec).

### 5.5 Reminders

Ticket holders now get the spec's **two** reminders:
- the day before;
- **2 hours before**, which is new.

Both are `event_reminder` and both also go by email. The day-before reminder covers 2 to 24 hours before; the 2-hour one covers 10 minutes to 2 hours. A person never gets both at once.

### 5.6 Time zone from the location (B28)

When an event has `latitude` and `longitude`, its `timezone` is worked out from them on create and on every location change. This is an offline lookup. The `timezone` the phone sends is used only when there's no location.

**App changes:**
- [ ] None required. Keep sending `timezone` as a fallback, and keep showing the times in the event's `timezone`.

### 5.7 Server-side only (for awareness)

- **Deploy:**
  - migration `057_event_people_approvals`;
  - one new Python dependency, `timezonefinder`, installed by `deploy.sh` from `requirements.txt`.
- **No new settings.**

---

## Phase 6: Account and settings (B34, plus B32's Hide from my Collection)

### 6.1 Going public approves everyone waiting (`SettingsFollowRequests`)

`PATCH /api/user/me` with `{"profileVisibility": "public"}` on a private account now accepts **every** pending follow request in the same call. Each person gets the usual `follow` notification ("Your follow request was accepted"). Saving `public` again does nothing more.

**App changes:**
- [ ] `privacy_settings_page.dart`: remove the loop that approves requests one by one after going public. The server does it now.

### 6.2 Turning off selling (`SettingsSelling`)

`POST /api/user/me/seller/disable` no longer refuses because something is listed. Instead it:
- takes down fixed-price listings and auctions with no bids (the pieces stay, marked not for sale);
- answers `200` `{sellerEnabled: false, cancelledAuctions, delisted, skipped}`.

It is refused with **`409`** (before: `400`) only while:

| Situation | Message |
|---|---|
| An auction has bids | "An auction of yours has bids. Wait for it to close, or cancel it first." |
| A sale is still going through | "A sale of yours is still going through. Finish it first." |

**App changes:**
- [ ] Before confirming, say "Your listings and auctions without bids will come down." Show the `409` message as it is.

### 6.3 Deleting an account with work in progress (`SettingsDelete`)

`GET /api/user/me/deletion-check`:

```json
{"canDelete": false,
 "blockers": [{"kind": "hosted_event_tickets", "count": 1,
               "message": "You're hosting an event with tickets sold. Cancel it first (everyone is refunded)."}]}
```

| `kind` | Meaning |
|---|---|
| `running_auction` | One of their auctions has bids |
| `bidding` | They're bidding on an auction that hasn't finished |
| `shipping_sale` | A piece they sold is still on its way (or its money is held) |
| `shipping_purchase` | A piece they bought is still on its way |
| `pending_payout` | Money is still on its way to them (an art payout, or a ticket payout for a finished event) |
| `hosted_event_tickets` | An upcoming event they host has tickets sold |
| `fee_debt` | They owe card fees from an event they cancelled. Also carries `amountCents` |

`DELETE /api/user/me` (still with `{"password"}`) now refuses with **`409`** and the first blocker's message while the list isn't empty.

Listings and auctions without bids no longer block deletion; they're taken down as part of deleting. Past events that had tickets are kept, under the anonymized host, because buyers' tickets and receipts point at them.

**App changes:**
- [ ] `delete_account_page.dart`: read the list from `/me/deletion-check` instead of building it from listings, sales and purchases. Disable Delete while `canDelete` is false, and show each `message`.
- [ ] Confirming still takes the password. B34 asks whether typing DELETE should replace it; that's the client's decision.

### 6.4 Hide from my Collection (B32)

| Method | Path | Result |
|---|---|---|
| POST | `/api/collection/:pieceId/hide` | `200` `{pieceId, hidden: true}` |
| DELETE | `/api/collection/:pieceId/hide` | `200` `{pieceId, hidden: false}` (undo) |

- Only a piece the caller collected can be hidden; anything else gets `404`.
- A hidden piece leaves `GET /api/users/:username/collection` for everyone, the owner too.
- The owner can ask for `?includeHidden=1` to see everything. On the owner's own Collection, each item carries `hidden: bool`.
- The purchase itself is untouched, and `collectedCount` still counts it.

**App changes:**
- [ ] `PieceCollectedOwner`: show "Hide from my Collection", with Undo.
- [ ] Show the owner's hidden pieces with `?includeHidden=1` so they can bring one back.

B32's other half, the delivery card (`viewerOrder`, `trackingUrl`), comes with Convelio shipping (Phase 8).

### 6.5 Seller terms with the account's own rate

`GET /api/user/me/seller-terms`:

```json
{"artCommissionBps": 1500, "artCommissionPercent": 15.0, "ticketFeeBps": 800,
 "ticketFeePercent": 8.0, "reducedRate": true, "reducedRateUntil": "2027-06-01T00:00:00+00:00",
 "buyersPayFees": false, "termsVersion": "…", "termsUrl": "/terms"}
```

**App changes:**
- [ ] `SettingsSelling` "Includes fees": show the rates from here, and never hard-code them (spec).
- [ ] Don't say "founding artist" (spec). `reducedRate` is there if the screen needs to know.

The Terms page itself doesn't list fees. Adding them is legal text for the client to approve.

### 6.6 Server-side only (for awareness)

- **Deploy:** migration `058_collection_hidden`.
- **No new settings.**

---

## Phase 7a: Auctions v2, the 9 Nov design (B20)

Built to the canvas update of 9 Nov (`HandoffBidding`, `NoteBidVerify`, `NoteBidSheet`, `NoteBidPaymentFailed`, `NoteBidSecondChance`).

Two things are unchanged:
- **Money is held until delivery.** Every charge lands on Studio 3's account and the artist is paid after delivery, as before. There are no destination charges (decision A2).
- **Shipping, insurance and tax are still paid at `auction-checkout`** after a win, for now. Phase 7b (after Convelio shipping, Phase 8) folds them into the charge at close and the checkout goes away.

### 7a.1 Bidding: a card check, not a hold

`POST /api/pieces/:id/bids` keeps its request, plus an optional `addressId` (the design's Ship to row):

```json
{"amountCents": 32500, "paymentMethodId": "pm_…", "addressId": "…"}
```

- **No money is held any more.** The first bid only checks that `paymentMethodId` is a card saved to this account. Save it first with the existing `POST /api/payments/setup-intent` and PaymentSheet, where the bank verifies it.
  - A card that isn't saved to them gets `400` "Save this card first…".
  - Nothing is charged unless they win.
- Raising your own bid reuses the card and address. Send new ones to change them.
- `addressId` must be one of the bidder's addresses (`404` otherwise). It's stored now for Phase 7b.
- **Increments are the design's.** The step is based on the current high bid:

  | Current high bid | Step |
  |---|---|
  | Under $500 | $25 |
  | $500 to $999 | $50 |
  | $1,000 to $4,999 | $100 |
  | $5,000 to $9,999 | $250 |
  | $10,000 and up | $500 |

  `minNextBidCents` and `bidIncrementCents` in the summary already reflect these.
- **Fixed end time:** no extensions. A late bid no longer pushes the end. The summary carries `fixedEndTime: true`. (Auctions that started before this release keep their old setting.)
- The outbid notification now says "Nothing is charged unless you win", not "your hold still stands".

**App changes:**
- [ ] First bid (`NoteBidVerify`): use PaymentSheet with Apple Pay through a SetupIntent, and add the Ship to address row. Send `paymentMethodId` and `addressId`.
- [ ] Remove any "hold" wording.
- [ ] Use the server's `minNextBidCents`. Quick picks step by the table above.
- [ ] Don't show "extends if someone bids late".

### 7a.2 When the auction ends: the saved card is charged

At the close, the winner's saved card is charged **off-session** for the hammer price.
- The winner gets `auction_won`: "You won … and your card has been charged. Confirm your delivery details to finish."
- The auction goes to `awaiting_winner`, the same state the app already handles for checkout.

### 7a.3 Card fails: 12 hours to fix it (`NoteBidPaymentFailed`)

If the charge fails (a bank verification request counts as a failure, because nobody is there to answer it):
- The auction goes to `awaiting_payment`.
- The winner gets `auction_payment_failed` with `payload.deadlineAt`, and the seller gets `auction_winner_payment_failed`.

The winner then has **12 hours**. The summary shows them:
- `awaitingPayment: true`;
- `winnerDeadlineAt`, the end of the window, which **never moves** however many cards are tried (BE-4);
- `paymentRetryAt`, the next automatic retry (at 1, 4 and 8 hours), or `null`.

`POST /api/pieces/:id/auction/retry-payment` with `{"paymentMethodId"}` charges a new card straight away:
- `200` means paid.
- `402` means that card was declined too; they can try another until the deadline.

**App changes:**
- [ ] Payment-failed screen: a countdown to `winnerDeadlineAt`, "We'll try again at `paymentRetryAt`", and "Use another card", which saves a card and then calls `retry-payment`.

### 7a.4 Second chance (`NoteBidSecondChance`)

When the 12 hours run out, the winner forfeits (notification `auction_forfeited`, nothing charged). The **next bidder whose top bid met the reserve** is offered the piece at that bid:

- The auction status becomes **`second_chance`**, which is new.
- The bidder gets `auction_second_chance` (also by email), with `payload: {amountCents, expiresAt, offerId}`. The seller gets `auction_second_chance_sent`.
- The offered bidder's summary carries `secondChanceOffer: {amountCents, expiresAt, lastError}`. It's `null` for everyone else.

| Method | Path | Body | Result |
|---|---|---|---|
| POST | `/api/pieces/:id/auction/offer/accept` | optional `{"paymentMethodId"}` | Charges their saved card (or this one) at once. `200` with the summary: they're the winner (`isWinner`, `awaiting_winner`). `402` if declined: the offer stays open until it expires, and `lastError` says why |
| POST | `/api/pieces/:id/auction/offer/decline` | — | `200`. The offer passes to the next bidder |

- An offer lasts **24 hours**. Unanswered, it passes on, and that bidder gets `auction_second_chance_expired`.
- If nobody is left, or the next bid is under the reserve, the auction goes to `needs_seller_action`. The seller gets `auction_needs_action`, and the piece comes off sale, as before.
- Nobody is ever charged without saying yes. The old automatic charging of the runner-up is gone, except for auctions still on holds from before this release.

**App changes:**
- [ ] The Second chance screen: the amount, a countdown to `expiresAt`, and "Accept and pay" (call accept, then open the collected screen). Handle `402` with "Use another card".
- [ ] Handle `auction_second_chance` and `auction_second_chance_expired`: open the piece.
- [ ] Treat the `second_chance` status as "not available" on the piece page for everyone else.

### 7a.5 Abandoned auction checkout (PAY-3)

`POST /api/pieces/:id/auction-checkout` no longer marks the auction sold. It becomes `closed_sold` only when the checkout order is paid.

If the winner abandons the checkout (the 15-minute sweep cancels the order), the piece goes back to them as `auction_won`, and they can check out again. Before this fix it was delisted and their hammer price was stuck.

**No app change.**

### 7a.6 Server-side only (for awareness)

- **Deploy:**
  - migration `059_auctions_v2`;
  - two new optional settings: `AUCTION_PAYMENT_FIX_HOURS` (default 12) and `AUCTION_SECOND_CHANCE_HOURS` (default 24).
- **Event auctions now get the same 12 hours.** The old rule was 10 minutes at an event.
- **Auctions already running** when this deploys keep their holds: their winner is captured from the hold, and their runners-up cascade as before.
- **Local development (no Stripe):** the card id `pm_card_chargeDeclined` declines, as it does in Stripe's test mode, so the failure flows can be tried.

---

## Phase 8: Convelio shipping and checkout pricing (B19, B31, B32)

Built to the Convelio plan and Convelio's v2 API. **Convelio is off until the server switches it on** (`CONVELIO_ENABLED`). Until then, checkout keeps the flat rates and the current app works unchanged.
- **Switching on:** only after the app below ships. With Convelio on, checkout needs a `quoteId`.
- **Checking which mode is on:** `GET /api/pieces/:id/shipping-quote` returns `live: true` or `false`.

### 8.1 What the artist fills in (listing, `SettingsSelling`)

Convelio prices on the **packed** piece. Before a piece can be shipped, the piece and the artist need:

| Field | Notes |
|---|---|
| `packageLengthCm`, `packageWidthCm`, `packageHeightCm` | Packed size. Collect it in inches and convert |
| `weightKg` | Packed weight. Collect it in pounds and convert |
| `declaredValueCents` | Optional; the price is used otherwise |
| `shippingItemType` | One of `fine_art.painting`, `fine_art.picture`, `fine_art.sculpture`, `fine_art.installation`, `decorative.ceramics`, `decorative.artefact`, `other.other`. If not sent, it's guessed from the medium |
| `shippingPacking` | `not_packed` (Convelio crates it, the default) or `wood_crated` |
| `shippingMaterials` | A list from `glass`, `stone`, `ceramic`, `framed_glass`, `unframed`, `pastel`, `wet`, `protruding`, … |
| **Pickup address** (on the artist, B31) | `PUT /api/user/me/pickup-address {"addressId"}`, one of their own **US** addresses **with a phone** (Convelio calls it to arrange pickup). `GET` returns it. Only ever shown to the artist |

The piece's owner sees `shippingReady: {ready, missing: ["weight", "pickup address", …]}` on the piece detail.

**App changes:**
- [ ] Listing form: packed size in inches, weight in pounds, packing, item type, and materials or framing.
- [ ] A Pickup address screen in Settings, Selling.
- [ ] Show `shippingReady.missing` to the artist.

### 8.2 The piece page estimate (B19)

`GET /api/pieces/:id/shipping-estimate?zip=&state=` (no sign-in needed):

```json
{"available": true, "source": "table", "lowCents": 25000, "highCents": 60000, "currency": "USD", "live": false}
```

- `source: "convelio"` when Convelio prices it; for now it doesn't for US routes, so `source` is `"table"`, a range from the packed size and weight.
- `available: false` (with `missing`) when the artist hasn't added the size and weight.

**App changes:**
- [ ] "Shipping from about $250 to $600" on the piece page.

### 8.3 Checkout with a live quote (`collect_piece_sheet.dart`)

**1. Quote.** `POST /api/pieces/:id/shipping-quotes`:

```json
{"addressId": "…", "speed": "regular_speed" | "express", "deliveryType": "curbside" | "white_glove", "insurance": true}
```

`insurance` defaults to on. The response is `201`:

```json
{"id": "…", "status": "created", "pending": false, "speed": "regular_speed", "deliveryType": "curbside",
 "insurance": true, "shippingCents": 34000, "insuranceCents": 2500, "totalShippingCents": 36500,
 "currency": "USD", "reference": "QVO-…", "expiresAt": "…"}
```

- The call takes **2 to 9 seconds**. Show a loading state and use a 30-second timeout.
- **`pending: true`** (`status: "processing"`) means Convelio is pricing it by hand, within 24 hours. The prices are `null`. Show "Shipping quote on its way: we'll notify you".
  - Nothing is reserved. The buyer gets `shipping_quote_ready` (a push, target the piece, `payload.quoteId`).
  - `GET /api/shipping-quotes/:id` then shows `status: "ready"` with prices.
- **`409`** with a message when the artist hasn't added what Convelio needs.
- **`400`** for a non-US address.

**2. Order.** `POST /api/pieces/:id/collect` with `{"addressId", "quoteId"}`. The server takes the price from the quote; the app never sends one.

| Case | Status and message |
|---|---|
| The quote is still pending | `409` "This shipping quote is still being priced…" |
| The quote has expired | `409` "…has expired. Get a new one." (expires after 24 hours for now) |
| The quote is for another address | `409` |
| Convelio on, no `quoteId` | `400` |

The order carries:
- `shippingCents`: shipping plus insurance;
- `insuranceCents`: the insurance part of that;
- `deliveryType`;
- `taxCents`.

- **A piece collected in the room at an event:** send `"shippingMethod": "pickup"` instead of a quote (only allowed for event pieces with pickup delivery).
- **Auction checkout** (`/auction-checkout`) takes the same `quoteId`.

**3. Payment.** As now. After the payment goes through, Studio 3 books the courier with Convelio. Nothing is booked before payment.

**App changes:**
- [ ] Replace the 4 flat tiers with Regular or Express, Curbside or White-glove, and an insurance toggle.
- [ ] Show the quote's shipping and insurance on separate lines.
- [ ] Add loading, error and pending states.
- [ ] Handle the `shipping_quote_ready` notification by opening checkout again on that quote.

### 8.4 Tracking (B32)

Order responses and the piece's `viewerOrder` carry the shipment:

```json
"shipment": {"provider": "convelio", "providerStatus": "out_for_delivery", "status": "in_transit",
  "trackingUrl": "https://…", "reference": "CVO-…", "deliveredAt": null, "onHold": false, "cancelled": false,
  "steps": [{"key": "booked", "done": true}, {"key": "picked_up", "done": true},
            {"key": "in_transit", "done": true}, {"key": "out_for_delivery", "done": true},
            {"key": "delivered", "done": false}]}
```

- **On a collected piece:** `GET /api/pieces/:id` adds `viewerOrder: {id, side: "buyer" | "seller", status, deliveryType, shipment, pickupAt, arrivalWindow, onTime}` for the buyer and the artist; `null` for everyone else.
- **Not available yet:** `pickupAt`, `arrivalWindow` and `onTime` stay `null`. Convelio arranges pickup with the artist by phone and gives no delivery estimate in its API.
- **The order moves by itself:**
  - Convelio's "shipment created" or "picked up" → `shipped`;
  - "delivered" → `awaiting_confirmation`, and the buyer gets `order_delivered`.
- **Buyer notifications:**

  | Type | When |
  |---|---|
  | `shipment_picked_up` | The courier collected it |
  | `shipment_out_for_delivery` | It's out for delivery |
  | `order_delivered` | Delivered |
  | `shipment_on_hold` | Convelio put it on hold |
  | `shipment_canceled` | The shipment was cancelled |

- **Auto-complete:** if the buyer never taps "Confirm received", the order completes **7 days after delivery** (unless they reported an issue), and the artist is paid.

**App changes:**
- [ ] Order detail and the collected-piece card: a tracker from `steps`, the reference, and "Track shipment" opening `trackingUrl`.
- [ ] Show "Confirm received" after delivery, with "or it completes automatically in 7 days".

### 8.5 Server-side only (for awareness)

- **Settings** (all optional):
  - `CONVELIO_ENABLED`, `CONVELIO_API_KEY` (`CONVELIO_BASE_URL` is optional);
  - `SHIPPING_QUOTE_VALID_HOURS` (24) and `SHIPPING_INSURANCE_DEFAULT` (true);
  - `ORDER_AUTO_COMPLETE_DAYS` (7);
  - `SALES_TAX_PROVIDER`: `flat`, the default, until the accountant agrees to `stripe`.
- **Webhook:** `POST /api/webhooks/convelio`, signature-checked. Each event is stored first, then processed. `scripts/register_convelio_webhooks.py` registers it for all five events.
- **Booking safety:** a booking whose result we never heard back about is never retried automatically, so a piece can't be booked with two couriers. The admin console lists failed and unknown bookings with Book again.
- **Tax:** with `SALES_TAX_PROVIDER=stripe`, Stripe Tax works out the tax on the artwork and the shipping from the delivery address.
- **Deploy:** migration `060_convelio_shipping`.
- **Also fixed:** a server bug where a "run after commit" job could start too early, when a savepoint committed partway through a transaction. It affected wallet updates and waitlist notifications too.

---

## Phase 7b: No checkout after winning (the 9 Nov design)

Once the hammer price has been charged at close (7a), the rest is charged to the **same saved card**, with nothing for the winner to do:
1. Convelio is asked for a quote to the winner's **Ship to** address, the `addressId` sent with their first bid (7a.1).
2. **Instant price:** the winner's order is created and shipping, insurance and tax are charged at once. The courier is booked exactly as for any sale (8.3, 8.4).
3. **Custom quote** (Convelio prices it by hand within 24 hours): the winner gets `auction_shipping_pending` ("Shipping price on its way"). The charge happens automatically when Convelio's price arrives; no checkout to come back to.
4. **That charge fails:** the order is called off and the piece goes back to the winner. They get `auction_shipping_failed` ("Finish your purchase") and complete at the existing `POST /api/pieces/:id/auction-checkout` with another card.

When it's done the winner gets `auction_shipping_charged` ("Delivery booked"), and the piece shows its `viewerOrder` with tracking (8.4).

**Not used** (the winner checks out as before, at `auction-checkout`) when:
- the piece is collected at the event (`deliveryMode: "pickup"`);
- the winner gave no Ship to address;
- the artist hasn't added the packed size, weight or pickup address;
- or Convelio is off.

**While the auction is settling,** the winner's auction summary carries:

```json
"autoCheckout": {"state": "pending_quote" | "charged" | "failed" | "manual", "orderId": "…" | null}
```

It's `null` for everyone else.

| `state` | What to show |
|---|---|
| `pending_quote` | "Shipping price on its way" |
| `charged` | The order (`orderId`) |
| `failed` | "Finish your purchase", opening the auction checkout |
| `manual` | The auction checkout, as today |

Once the order is paid, the auction is sold, and the piece's `viewerOrder` (8.4) is the place to read it.

**App changes:**
- [ ] After winning, don't open checkout when `autoCheckout.state` is `pending_quote` or `charged`. Show the shipping-pending note, or the order.
- [ ] Open the auction checkout only for `failed` or `manual` (or no `autoCheckout`).
- [ ] Handle the `auction_shipping_pending`, `auction_shipping_charged` and `auction_shipping_failed` notifications: open the piece.
- [ ] Make sure the first bid sends `addressId` (7a.1). Without it, the winner falls back to checkout.

**Server-side only (for awareness):**
- **Deploy:** migration `061_auction_auto_checkout`. No new settings: it runs whenever `CONVELIO_ENABLED` is on.
- **Same payment path:** the follow-on charge is an ordinary order payment (metadata `kind: auction_balance`), so refunds, payouts and the ledger treat it like any sale.
