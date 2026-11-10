# Frontend handoff

_Re-checked line by line against the backend code on 10 Nov 2026._

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
| 7b. Shipping, insurance and tax in the charge at close | Done, on `development` (`c8b0f19`); runs wherever Convelio is on | Show the automatic charge; keep the auction checkout only as the fallback |
| 8. Convelio shipping and checkout pricing | Done, on `development` (`5fea30f`); on by default wherever a Convelio key is set | New checkout (speed, delivery type, insurance, live quote, pending quote); listing fields; pickup address screen; shipment tracker; piece-page estimate |

The shared dev database (Aiven) is at migration `061_auction_auto_checkout`, so a build pointed at it can use everything through Phase 7b.

---

## Phase 1: Security

**Shapes and endpoints:** no endpoint or response shape changed. Three behaviours did.

### 1.1 Sign-in, sign-up and code screens: new limits

| Endpoint | New behaviour | Status and message |
|---|---|---|
| `POST /api/auth/login` | At most 20 attempts a minute from one network address, on top of the existing per-account lock | `429` "Too many requests. Please try again later." |
| `POST /api/auth/otp/verify` (all purposes, **now including sign-up**) and `POST /api/user/me/email/confirm-change` | At most 20 checks a minute from one address (the two share one counter) | `429` (same message) |
| `POST /api/auth/register` | At most 5 a minute from one address | `429` (same message) |
| `POST /api/auth/otp/verify`, `POST /api/auth/register`, `POST /api/user/me/email/confirm-change` | A code is thrown away after **5 wrong guesses** (before, this applied only to login and reset codes). Each kind of code also allows at most 30 wrong guesses an hour: sign-up/register, login, reset and email change are counted separately for the same email | `400`, the same "verification failed" message as a wrong code |
| `login`, `register`, `otp/verify`, `email/confirm-change` | If the server's Redis is down, these refuse instead of running without limits | `503` "Sign-in is temporarily unavailable. Please try again shortly." |
| Any call that sends or checks a code | Redis fails while the code is being stored or read | `503` "OTP service is temporarily unavailable. Please try again in a moment." |

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

Same endpoint and body as today. The artist may also send an optional `"reason"`, kept with the refund (cut to 2,000 characters; without one the refund records "Cancelled by the artist."). `200` returns the order, as `GET /api/orders/:id`.

| Order status | Who | Result |
|---|---|---|
| `pending_payment` | Buyer or artist | `200`, order `status: "cancelled"`. The server cancels the Stripe payment first |
| `pending_payment`, payment already going through | Buyer or artist | `409` "This payment is already going through. Check back in a moment." The order stays as it is; reload it shortly (it will become `paid`) |
| `paid` | **Artist** | `200`, order **`status: "refunded"`** (not `cancelled`). The buyer is refunded in full and notified "Order cancelled" |
| `paid` | **Buyer** | `403` "A paid order can't be cancelled by the buyer. If something's wrong, report an issue and we'll sort it out." |
| `paid`, refund failed at Stripe | Artist | `502` "We couldn't refund the buyer just now. The order is on hold and our team will finish the refund — you don't need to do anything." The order stays `paid` and the artist's payout is held. Show the message as it is; Studio 3 retries from the admin console |
| `paid`, the artist's payout has already gone out | Artist | `409` "The artist has already been paid for this order; a refund here would leave the platform out of pocket. Recover the transfer in Stripe first." Rare; show a generic "Contact support" rather than this staff text |
| `shipped` or later | Anyone | `409` "Cannot cancel an order in status 'shipped'." (the status in the quotes is the order's own) |

Any other `status` value is still refused with `403` (this was already the case).

**App changes in `lib/screens/order_detail_page.dart`:**
- [ ] **Buyer** (`_buyerActions`): show "Cancel order" **only** for `pending_payment`. For `paid`, `shipped` and `awaiting_confirmation`, show **"Report an issue"** instead, using the existing `OrderService.reportIssue`.
- [ ] **Artist** (`_sellerActions`): keep "Cancel order" for `pending_payment` and `paid`. For `paid`, show a confirmation first: "Cancel this sale? The buyer will be refunded in full and the piece goes back on sale."
- [ ] **Artist:** remove "Mark as shipped" and "Mark as completed". The backend already refuses both with `403`; shipping is updated by Studio 3 (and later by Convelio), and completion comes from the buyer's "Confirm received".
- [ ] Treat a `refunded` status like `cancelled` in the order list and detail, labelled "Refunded".

### 2.2 Notifications

Inbox type `order`, `targetType: "order"`, `targetId` = the order ID, `payload: {}` (plus the usual `message`). A tap should open the order.

| When | Who | Title | Body |
|---|---|---|---|
| The artist cancels a paid order | Buyer | "Order cancelled" | "The artist cancelled your order and you've been refunded in full. It may take a few days to appear." |
| An admin refunds an order (including resolving a dispute with a refund) | Buyer | "Refund issued" | "We've refunded your order in full. It may take a few days to appear." |
| An admin resolves a dispute in the artist's favour | Artist | "Payment released" | "The issue on your sale was resolved and your payment is on its way." |

A late payment that is refunded automatically (2.3) sends no notification.

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

`GET /api/user/me/commission-quote?priceCents=<n>&saleKind=art|ticket` adds `reducedRateUntil`: an ISO date-time when the reduced rate ends, or `null` when it has no end date or there's no reduced rate.
- `priceCents` is required (`400` "priceCents is required and must be a whole number of cents."; negative is `400` "priceCents cannot be negative."). `saleKind` is optional and defaults to `art`; anything else is `400` "saleKind must be one of: art, ticket."
- The full response: `{priceCents, saleKind, commissionBps, commissionCents, netCents, reducedRate, reducedRateUntil, coversProcessingFee, breakEvenCents}`.
- After that date, `reducedRate` is `false` and the standard rate applies.
- Orders and auctions keep the rate they were sold at.

**App changes:**
- [ ] Optional: when `reducedRateUntil` is set, add "until <Month Year>" to the "15% founding artist commission" line.

### 2A.4 Auction fixes (no app change needed)

- **"Update your card" notification:** `bid_hold_expired` ("Update your card to stay in this auction", `payload: {amountCents, bidId}`) now has `targetType: "piece"` and `targetId` set to the piece, so a tap opens the auction. Before, it opened nothing. It only applies to auctions still on holds from before Phase 7a.
- **Bidding again after a card hold expired:** this no longer leaves two active bids. The earlier one becomes `outbid`.
- **Cancelling an auction that hasn't opened** (status `draft`, including one set to open later, such as an event auction before the doors) with `POST /api/pieces/:id/auction/cancel` now actually cancels it. Before, it answered 200 and left it in place.
  - `200` returns `{pieceId, auctionId, cancelledBids, status}` (`status` is the piece's new status). `cancelledBids` is `0` for a draft.
  - No running or draft auction: `409` "This piece has no auction running."
- **`docs/API.md`** now lists every status relist accepts: `needs_seller_action`, `closed_no_bids` and `closed_reserve_not_met`.

---

## Phase 3: Ticketing core (B22, plus age limit and visibility from B28)

Built to the ticketing spec of 25 Oct 2026. The endpoints the app already calls on `origin/main` (`/orders/quote`, `/orders`, `/user/me/tickets`, the event detail fields) keep exactly the shapes `lib/models/event_order.dart` and `studio_event.dart` read. Most of the buyer side works with no app change.

### 3.1 New fields on events

**Event card** (every list), added:
- `isFree`: now real. `false` when any open ticket type costs money.
- `ticketCount`: how many tickets the viewer holds.
- `ageLimit`: `"all"`, `"18"` or `"21"`.
- `rescheduledAt`: ISO, or `null` if never moved (Phase 4, 4.4).
- `waitlist`: only when the viewer is in line (Phase 4, 4.2); left out otherwise, and never on the host's own cards.

**Event detail** (`GET /api/events/:id`), added:

| Field | Type | Notes |
|---|---|---|
| `ticketTypes` | array | See 3.2. Empty for an event with no ticket types (free RSVP event) |
| `refundPolicy` | `"7d"` / `"24h"` / `"none"` | |
| `refundPolicyText` | string | e.g. "Refunds up to 7 days before the event." |
| `visibility` | `"public"` / `"link"` | Link-only events never appear in browse, search, feeds, Explore or profiles, but open from their link |

`waitlist` arrives in Phase 4 (see 4.2).

**Create and edit** (`POST /api/events`, `PATCH /api/events/:id`) accept:
- `refundPolicy`, `ageLimit` and `visibility`, with the values above. The defaults on create are `refundPolicy: "none"`, `ageLimit: "all"` and `visibility: "public"`. An empty or `null` `ageLimit` is saved as `"all"`.
- Anything else returns `400` "refundPolicy must be one of: 7d, 24h, none." (and the same pattern for `ageLimit`: all, 18, 21, and `visibility`: public, link).
- `refundPolicy` can't change once a paid ticket has sold (any order with money on it that is `paid` or `partly_refunded`): `409` "Paid tickets have sold under the current refund policy, so it can't change."
- `PATCH` on a cancelled or archived event: `409` "This event can no longer be edited."

### 3.2 Ticket types

Each entry in `ticketTypes`:

```json
{"id": "…", "name": "VIP", "priceCents": 2500, "includes": "Drinks",
 "left": 7, "perOrderLimit": 4, "saleStartsAt": null, "saleEndsAt": null,
 "onSale": true, "soldOut": false}
```

- `left` is `null` for no cap, and `0` when sold out. Show "Only N left" in red when it's 5 or fewer (spec).
- **The host's own view** (the event's owner only, not co-hosts) also gets `quantity`, `sold`, `hostNetCents` (what they receive per ticket, e.g. a $25 ticket = 2300), `closed` and `waiting` (4.2), and also lists closed types.
  - `sold` is **sold or held**: tickets that still admit someone, plus tickets held in unpaid checkouts (10 minutes) and tickets held for people on the waitlist. Label it "Sold or held", or just use `left`.
  - Co-hosts get the public shape, without closed types.

**Write endpoints** (the host or an accepted co-host; anyone else gets `404`). Only the host's response carries the extra fields above.

| Method | Path | Body | Notes |
|---|---|---|---|
| GET | `/api/events/:id/ticket-types` | — | `{ticketTypes}`. Open to anyone for a published event. On a draft, only the host can read it (`404` for everyone else, co-hosts included) |
| POST | `/api/events/:id/ticket-types` | `{name, priceCents, includes?, quantity?, perOrderLimit?, saleStartsAt?, saleEndsAt?, sortOrder?}` | `201` with `{id, ticketTypes}`. **Paid types need a seller profile** (the host's, even when a co-host adds the type): otherwise `403` "Set up your seller profile to sell paid tickets, so we can pay you.", and the app should open seller setup. `maxPerOrder` is accepted as another name for `perOrderLimit` |
| PATCH | `/api/events/:id/ticket-types/:typeId` | any of the above | `200` `{ticketTypes}` |
| DELETE | `/api/events/:id/ticket-types/:typeId` | — | `200` `{ticketTypes, result}`. `result: "deleted"` when no order ever included the type; otherwise `"closed"` (even if the only orders were expired or failed checkouts), and its tickets stay valid |

Ticket-type errors:

| Status | Message |
|---|---|
| `400` | "A ticket type needs a name." / "Keep the ticket name under 80 characters." |
| `400` | "A paid ticket must be at least $0.50." |
| `400` | "priceCents must be a whole number." / "quantity must be at least 1." (same pattern for `perOrderLimit`, `sortOrder`) |
| `400` | "saleStartsAt must be an ISO 8601 date-time." / "saleStartsAt must include a time zone offset." (same for `saleEndsAt`) |
| `400` | "A sale has to end after it starts." |
| `400` | "An event can have at most 20 ticket types." |
| `404` | "Ticket type not found." |
| `409` | "This ticket has sold, so its price can't change. Add a new ticket type instead." |
| `409` | "N of these are already sold or held, so the quantity can't go below that." |
| `409` | "This event can no longer be edited." (POST and PATCH on a cancelled or archived event) |

**App changes:**
- [ ] Hosting, Tickets step: replace the "Free RSVP with a cap" screen with ticket types: name, price or free, what's included, quantity, max per order, sale start and end.
- [ ] Show "You receive $X per ticket" from `hostNetCents` (the host only; a co-host's screen won't have it, nor `sold` or closed types).
- [ ] Add pickers for refund policy, age limit (Details), and public or link-only (Details).

### 3.3 Buying

**Quote:** `POST /api/events/:id/orders/quote` with `{"items": [{"ticketTypeId", "quantity"}]}` returns (no sign-in needed):

```json
{"subtotalCents": 5000, "serviceFeeCents": 0, "taxCents": 413, "totalCents": 5413,
 "refundPolicy": "Refunds up to 7 days before the event.",
 "items": [{"ticketTypeId": "…", "name": "VIP", "quantity": 2, "unitPriceCents": 2500, "lineCents": 5000}]}
```

**Buyers never pay a fee:** `serviceFeeCents` is always `0` (spec). The quote holds nothing; stock is checked again when the order is placed.

**Order:** `POST /api/events/:id/orders` with the same `items`, plus an optional `email` (where the tickets are sent; the account email by default).

| Case | Response |
|---|---|
| Paid | `201` `{id, orderId, clientSecret, status: "pending_payment", totalCents, holdExpiresAt}`. The tickets are **held for 10 minutes**. Present the PaymentSheet with `clientSecret` |
| Free only | `201` with `clientSecret: null` and `status: "paid"`: the tickets are issued already |
| Development server without Stripe | `201` with `clientSecret: null`, `status: "paid"` and `devMode: true` for a paid order. Skip the PaymentSheet |
| Stripe couldn't start the payment | `502` "We couldn't start the payment. Please try again." The hold is released |

**Refusals** (the same for the quote and the order; show the message):

| Status | Message |
|---|---|
| `400` | "Choose at least one ticket." / "An order can have at most 50 tickets." / "Unknown ticket type." / "Each item needs a ticketTypeId and a quantity." |
| `400` | "You're hosting this one." (the host buying their own tickets) |
| `409` | "Tickets for this event aren't on sale." (not published) |
| `409` | "This event has already ended." |
| `409` | "One of those tickets isn't available for this event." |
| `409` | "VIP isn't on sale right now." (closed, or outside its sale window) |
| `409` | "You can buy up to 4 VIP per order." |
| `409` | "VIP is sold out." / "Only 2 VIP left." |

After the PaymentSheet succeeds, the tickets are issued by the server when Stripe confirms. This usually takes a second or two.

**Poll:** `GET /api/events/:id/orders/:orderId` (buyer only) returns:

```json
{"id": "…", "eventId": "…", "status": "paid", "subtotalCents": 5000, "taxCents": 413, "totalCents": 5413,
 "email": "…", "holdExpiresAt": "…", "paidAt": "…", "ticketCount": 2, "createdAt": "…",
 "refundedCents": 0, "refundable": true, "refundableUntil": "…",
 "walletPasses": {"apple": true, "google": true}, "walletPassAvailable": true}
```

There is no `orderId` key here; use `id`. `ticketCount` counts only tickets that still admit someone.

Keep polling while `status` is `pending_payment`. **Stop** on any of:
- `paid` (or `partly_refunded`): open the confirmation;
- `expired`: the 10 minutes ran out; the tickets went back on sale;
- `failed`: the payment failed; offer to try again (a new order);
- `cancelled`: the event was cancelled or ended while paying; any money is refunded automatically.

Order statuses: `pending_payment`, `paid`, `expired`, `cancelled`, `failed`, `refunded`, `partly_refunded` (Reference).

**My tickets:** `GET /api/user/me/tickets?eventId=` returns `{tickets: [{id, eventId, orderId, code, ticketTypeName, number, status, scannedAt, refundable, walletPasses, walletPassAvailable}]}`.
- `code` is the value to draw as the QR. It's signed, so a made-up code won't pass the door.
- `number` is a short code like `S3-T-8KQ2XM`, for typing at the door.
- `status` is only ever `valid` or `checked_in`: refunded and voided tickets aren't listed.
- RSVP and capacity tickets (no ticket type) have `ticketTypeName: "General Admission"`.

**App changes:**
- [ ] `EventCheckoutPage`: after `presentPaymentSheet`, poll `GET …/orders/:orderId` (about every 1 second, for up to 20 seconds) and open the confirmation once it's `paid`, instead of only reloading the event. Stop on `expired`, `failed` or `cancelled` too, and say what happened.
- [ ] Optionally show a countdown from `holdExpiresAt`.
- [ ] **Free tickets:** when the choice is free, call `POST /api/events/:id/orders` with the chosen `items`, not `setRsvp`. RSVP still works (3.4), but it can't say how many or which type, so a choice of 2 free tickets only gets 1 through RSVP.
- [x] `EventTicketPage`, `my_events_page.dart` (`ticketCount`) and `event_order.dart` already read the right fields.

### 3.4 The free one-tap RSVP

`POST /api/events/:id/rsvp {going}` keeps its request and response (`200` `{going, rsvpCount, spotsLeft, isFull}`), so installed apps keep working. A draft is `404` to anyone but its host. Going is refused like an order when the event isn't on sale or has ended (`409`, 3.3), or for the host (`400`). Behind it:

| Event | `going: true` | `going: false` |
|---|---|---|
| No ticket types | Issues one **General Admission** ticket with a QR code, capped by the event's `capacity` as before. When full: `409` "This event is full. The host may open more places closer to the date." | Voids the free ticket |
| Has a free ticket type | Issues one ticket of the first free type with stock left. When every free type is gone: `409` "The free tickets are gone." | Voids the viewer's free tickets |
| Paid types only | `409` "This event needs a ticket. Choose tickets to continue." | — |
| Viewer holds paid tickets | — | Free tickets are voided, then `409` "Your paid tickets are still valid. You can ask for a refund from your ticket." Paid tickets are refunded from the ticket (4.1) |

A viewer who already holds a ticket and taps going again gets `200` with nothing new issued.

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
| `?platform=google` | `200` `{"data": {"saveUrl": "https://pay.google.com/gp/v/save/…"}}`. Open this URL (it shows Google's own "Save to Google Wallet" screen). One link holds every live ticket in the order |
| none, or `?platform=apple` | `200` raw bytes. With one ticket it's `application/vnd.apple.pkpass`; with several it's `application/vnd.apple.pkpasses` (a bundle, iOS 15+) |
| `&ticketId=<id>` (either platform) | Just that ticket: its `.pkpass`, or a Google link holding only it |
| Any other `platform` | `400` "platform must be apple or google." |

The order must be `paid` or `partly_refunded`. A part-refunded order still gets passes for the tickets it has left.

| Case | Status and message |
|---|---|
| Order `cancelled` or `refunded`, no live tickets left, or `ticketId` isn't a live ticket in this order | `410` "These tickets are no longer valid." |
| Order `pending_payment`, `expired` or `failed` | `409` "The tickets haven't been issued yet." Only `pending_payment` is worth waiting on (poll until `paid`, 3.3); `expired` and `failed` never get tickets |
| Google isn't set up on the server | `503` "Google Wallet isn't set up yet." |
| Apple isn't set up on the server | `503` "Apple Wallet isn't set up yet." |

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
- The link is `GET /api/wallet/apple/orders/<orderId>.pkpass?exp=…&sig=…`. A bad or expired signature gets `403`.
- Its errors are plain text, not the JSON envelope (the same 409/410/503 messages as 3A.2).
- The app doesn't need to call it; it's for the email.

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
- `refundableUntil` is `null` for policy `none`, and for any event that isn't published (a draft, a cancelled or an archived event).

**Endpoint:** `POST /api/events/:id/orders/:orderId/refund`, buyer only.

| Body | Result |
|---|---|
| `{}` | Refunds every unused ticket in the order |
| `{"ticketIds": ["…"]}` | Refunds just those tickets |

`200` returns the order (as `GET …/orders/:orderId`) plus `refund: {status, amountCents, ticketIds}`.
- `refund.status` is the order's new status: `partly_refunded` or `refunded`, or `already_refunded` (with `amountCents: 0`) when the order was already fully refunded.
- The amount is the tickets' price plus their tax; the last refund of an order returns exactly what's left.

| Case | Status and message |
|---|---|
| Outside the window, or `none` | `409` "Refunds for this event have closed." |
| The event was cancelled | `409` "This event was cancelled, so everyone is refunded automatically." |
| A checked-in ticket | `409` "A ticket that's been checked in can't be refunded." |
| A chosen ticket was already refunded or voided | `409` "Some of those tickets were already refunded." |
| Nothing left to refund | `409` "There's nothing left to refund on this order." |
| Free tickets | `409` "Free tickets aren't refunded; tap Not going to give them back." |
| An order that can't be refunded (unpaid, expired, failed, cancelled) | `409` "These tickets can't be refunded." |
| Already going through | `409` "A refund for this order is already going through." |
| A `ticketIds` entry that isn't an id | `400` "Unknown ticket." |
| A `ticketIds` entry from another order | `404` "Those tickets aren't in this order." |
| Stripe failed | `502` "The refund didn't go through at Stripe. We'll retry it; nothing more to do. (<reason>)". The tickets stay valid until the money is back |
| Someone else's order | `404` "Order not found." |

After a failed refund, the next refund call on that order retries the same refund: it ignores new `ticketIds` and doesn't check the window again.

After a refund, the tickets disappear from `GET /api/user/me/tickets`. Their QR codes no longer scan, the wallet pass turns void, and the tickets go to the waitlist first (4.2). When no tickets are left, the buyer is no longer `viewerIsGoing`.

Notification: type `ticket_refunded`, title "Refund issued", target the event, `payload: {orderId, amountCents}`: "We've refunded $21.65 for your tickets to … It may take a few days to appear." It's sent for buyer and admin refunds, not when the host cancels the event (the cancellation notice covers that).

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
- `waitingCount` and `waitingByType` are for the host only (the event's owner, not co-hosts).
- `waitlist` is left out when there's nothing to show (B22). The host always gets it while the event has ticket types, open or not.
- The host's ticket types also carry `waiting`: how many people want that type (owner only, too).

**Endpoints** (exactly the app's):

| Method | Path | Body | Result |
|---|---|---|---|
| POST | `/api/events/:id/waitlist` | `{"items": [{ticketTypeId, quantity}]}` | `200` with `waitlist`. Changing what you want keeps your place |
| DELETE | `/api/events/:id/waitlist` | — | `200` `{left: true}`. Anything held passes to the next person |
| POST | `/api/events/:id/waitlist/hold/release` | — | `200` with `waitlist`: "Keep waiting for [other type]". The held type is dropped from `wanted`; the others keep their place. If nothing else was wanted, they leave the line and the response is `{open: false, wanted: [], held: null}` (or the event's waitlist with an empty `wanted`, when it's open) |

| Status | Message | When |
|---|---|---|
| `409` | "VIP isn't sold out. Choose tickets instead." | Joining for a type that has tickets on sale |
| `409` | "You can wait for up to 4 VIP." | Over the type's per-order limit |
| `409` | "VIP isn't on sale any more." | The type was closed |
| `409` | "One of those tickets isn't available for this event." | A type from another event |
| `409` | "The waitlist for this event is closed." | Not published, or it has started |
| `400` | "You're hosting this one." | The host |
| `400` | "Choose at least one ticket." (and the other item errors of 3.3) | Bad `items` |
| `404` | "You're not on the waitlist for this event." | `hold/release` when not in line |
| `409` | "Nothing is held for you right now." | `hold/release` with no hold |

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
- The waitlist closes quietly when the event starts. When the event is cancelled it closes too, and everyone waiting gets `event_cancelled` (4.5).
- An event with no ticket types (a plain capacity RSVP) has no waitlist. Add a free ticket type to get one.

**Notifications** (target the event):

| Type | When | Message |
|---|---|---|
| `waitlist_hold` | Tickets are held for them. Also sent by email | Title "Tickets for <event>". "2 tickets are held for you until 9:40 PM CST." `payload.heldUntil` is the ISO time |
| `waitlist_hold_ended` | Their hold ran out | Title the event's name. "The tickets held for you have gone to the next person in line." |
| `event_tickets_available` (B26) | A sold-out event has tickets on sale again. Sent to people who saved it, except the host and anyone holding tickets, once per reopening. Not sent when it reopens within 10 minutes of selling out, which is just a checkout running out | Title the event's name. "Tickets are available again." |

**App changes:**
- [ ] A tap on `waitlist_hold` opens Choose tickets with `held.items` already picked and a countdown to `held.until`. This is already built; check the deep link.
- [ ] Handle the `event_tickets_available` and `waitlist_hold_ended` notification types: open the event.

### 4.3 Check-in at the door (`HostScanner`)

Only the host and co-hosts can use these; anyone else gets `404`. Phase 5 limits co-hosts to those who accepted their invite.

| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/api/events/:id/tickets/:code/check-in` | optional `{"scannedAt": ISO}` | `:code` is the QR value, the ticket number (`S3-T-8KQ2XM`, any case) or the ticket id (for the guest list's check-in by hand) |
| POST | `/api/events/:id/check-in/sync` | `{"scans": [{"code", "scannedAt"}]}` | Offline scans, up to 500. Returns `{results, checkedIn, total}`; `results` are in the same order as `scans`, and each carries its `code` |
| GET | `/api/events/:id/guests?q=&limit=` | — | Guest list, searchable by name, username or ticket number. `limit` defaults to 200, at most 1000 |
| GET | `/api/events/:id/check-in/manifest` | — | For offline use (below) |

All four need the event to be published or archived: otherwise `409` "This event isn't checking anyone in."

Request errors (`400`):
- "scannedAt must be an ISO 8601 date-time." (a time without an offset is read as UTC; a time in the future is cut to now)
- sync only: "scans must be a list.", "Sync at most 500 scans at a time.", "Each scan needs a code and a scannedAt."

Otherwise a scan answers `200` with a `result` to show, even for a bad ticket:

```json
{"result": "checked_in", "message": "Checked in: Ana Ruiz, VIP (1 of 2).",
 "ticket": {"id", "number", "ticketTypeName", "holderName", "holderUsername", "holderAvatarUrl",
            "position": 1, "of": 2, "status", "checkedInAt"},
 "checkedIn": 48, "total": 62}
```

| `result` | `message` |
|---|---|
| `checked_in` | "Checked in: [name], [type] (1 of 2)." The name is "Guest" when the holder has none. "1 of 2" counts every ticket in the order, including refunded or void ones. An offline scan that turns out earlier than the one on record returns just "Checked in." |
| `already_checked_in` | "Already checked in at 8:14 PM CST." |
| `invalid` | "Not a valid ticket." / "Not a valid ticket: it's for a different event." / "Not a valid ticket: it was refunded or cancelled." `ticket` is `null` except for the last |

- **Offline:** store each scan with its time and send them to `…/check-in/sync` when back online. The earliest scan of a ticket wins. If another device's earlier scan arrives later, this device's scan reads `already_checked_in` from then on.
- **Manifest:** `GET …/check-in/manifest` returns `{eventId, publicKey, codePrefix, ageLimit, generatedAt, tickets, checkedIn, total}`:
  - `publicKey` (Ed25519, standard base64 of the raw 32-byte key) and `codePrefix` (`S3T1.`), so the app can check a QR's signature without the network;
  - `tickets`: **only tickets that still admit someone** (`valid` or `checked_in`), each in the same shape as a scan's `ticket`. A correctly signed code that isn't in the manifest was refunded or voided: treat it as invalid;
  - `ageLimit` and the counts.
- **Offline code format:** a code is `S3T1.` followed by unpadded base64url of 97 bytes: the byte `0x01`, the ticket UUID (16 bytes), the event UUID (16 bytes), then an Ed25519 signature (64 bytes) over the first 33 bytes. Check the signature, check the event UUID is this event, then look the ticket id up in the manifest.
- **Guest list:** returns `{guests: [...], checkedIn, total, ageLimit}`, each guest in the scan's `ticket` shape, live tickets only. Show the age limit as a reminder (spec).

**App changes:**
- [ ] Manage event: replace "Guest list" with "Check in guests" (B28).
- [ ] The scanner shows the live count, the age limit and each scan result. It also needs Enter code and a guest list with search and check-in by hand.
- [ ] Offline: download the manifest when the scanner opens, queue scans with their time, and sync them. Offline, a signed code whose ticket isn't in the manifest is invalid (refunded or voided).

### 4.4 Reschedule (`HostReschedule`)

`POST /api/events/:id/reschedule` with `{"startsAt", "endsAt", "message"?}` (host only).

- `200` returns the event, with `rescheduledAt`, `rescheduleMessage` (cut to 500 characters), the new times and `refundableUntil` (the new start), plus `movedAuctions` and `guestsToNotify`.
- Every ticket holder (not the host) is told once:
  - notification `event_rescheduled`, title "Event rescheduled", `payload: {startsAt}` ("… has moved to Sat, Nov 28 · 8:00 PM CST. Your tickets still work.");
  - an email with the host's message.
- Tickets keep working, and refunds are open until the new start (4.1).
- Wallet passes show the new date.
- Both reminders (the day before and 2 hours before, 5.5) go out again for the new date.
- An auction on the event moves with it. **If bidding has started, the reschedule is refused.**
- `PATCH /api/events/:id` with new dates on a published event still answers `409` "A published event's date can't be changed here. Use Reschedule, which tells everyone with a ticket."

| Status | Message |
|---|---|
| `409` | "Only a published event can be rescheduled." |
| `409` | "This event has already ended." |
| `409` | "Bidding has started on a piece at this event, so it can't move. Cancel the event instead, or wait until the auction closes." |
| `400` | "The new start has to be in the future." |
| `400` | "That's the time it's already at." |
| `400` | "The new time is too short for the event's auction: bidding closes 30 minutes before the end." |
| `400` | The usual start/end checks (for example, the end must come after the start) |

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

**Confirm:** `POST /api/events/:id/cancel` with `{"message"?: "…"}` (host only). The existing `reason` field still works: without a `message`, `reason` is used as the message. The message is cut to 500 characters.

The response is the event plus:
- `ticketsVoided`
- `ordersToRefund` (a count)
- `refundTotalCents`
- `cardFeesCents`
- `waitlistCleared`
- `cancelledAuctions` and `delisted` (pieces taken down)
- `attendeesNotified`

On confirm:
- every ticket stops working at once, and wallet passes turn void;
- every paid order is refunded in full, in the background, within a minute;
- unpaid checkouts are called off, and a payment that slips through anyway is refunded;
- the waitlist is cleared;
- tagged pieces come off sale (as before);
- guests and everyone on the waitlist (not the host) get the `event_cancelled` notification (title "Event cancelled", target the event) and an email with the message. The body is `"<title>" has been cancelled. <message>`, or `"<title>" has been cancelled. Tickets are refunded in full.` with no message.

**Deleting:** `DELETE /api/events/:id` is refused while the event has any ticket order at all, even an unpaid or expired checkout: `409` "This event has tickets, so it can't be deleted. Cancel it instead: everyone is refunded and told." Drafts and events with no orders delete as before. Deleting a published event tells its guests with `event_cancelled` ('"<title>" has been removed by its host.').

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
- `viewerInvite`: `{roles, status}` when the viewer was ever asked. It stays after an answer, so a declined invite reads `status: "declined"`.
- `viewerCanEdit`: true for the host and accepted co-hosts. Left out for a signed-out viewer.

An invitee can open the event while it's still a draft, so they can see what they're saying yes to.

| Method | Path | Body | Result |
|---|---|---|---|
| POST | `/api/events/:id/invite/accept` | optional `{"role": "cohost"\|"artist"}` | `200` with the event plus `invite: {status, roles}`. Without `role`, it answers every invite waiting on this event. `404` "There's no invite waiting for you on this event." if none is waiting; `400` "Unknown participant role '<x>'." for a bad `role` |
| POST | `/api/events/:id/invite/decline` | same | Same as accept |
| GET | `/api/user/me/event-invites` | — | `{invites: [{role, invitedAt, event: <card>}]}`: invites waiting for an answer |

Putting a declined person back on the list asks them again.

**Accepted co-hosts can:**
- edit the event (`PATCH`);
- create, edit and remove ticket types (only the host's responses carry `quantity`, `sold`, `hostNetCents`, `closed` and `waiting`, 3.2);
- add and remove pieces;
- see the guest list and the QR codes;
- scan at the door.

**Only the host can:** publish, cancel, reschedule, delete, and change who is on the bill. A co-host calling any of these gets `404`.

**Notifications:**

| Type | Who | When | Message |
|---|---|---|---|
| `event_invite` | The invitee (also by email) | They're invited | Title "Event invite". "Ana invited you to be a co-host at Night Market." `payload: {role, eventTitle}` |
| `event_invite_response` | The host | The invitee answers | Title the event's name. "Mira accepted your invite to be a featured artist." `payload: {accepted, roles}` |

Both target the event.

**App changes:**
- [ ] An invites list from `/me/event-invites`, plus the `event_invite` notification, each opening the event with Accept and Decline.
- [ ] In Lineup and Manage, show `invites` with their status.

### 5.2 Artist approval of tagged pieces (`ArtistPieceRequest`)

A host (or co-host) can now tag **anyone's** piece in any mode (`featured`, `sale` or `bid`), with a price. Before, selling someone else's work was a `403`.

- **Someone else's piece starts `waiting`.** Its own listing or auction is untouched until the artist approves, and it's hidden from the event page.
- **Pieces the host owns**, or the person adding owns, are approved straight away, as before.

Lineup entries can carry three approval fields:
- `approvalStatus` (`waiting`, `approved` or `declined`);
- `listingEnded` (whether approving ended the piece's own listing);
- `respondedAt`.

Who sees them:
- **Event detail:** the host and co-hosts see every entry, each with the fields. The piece's artist sees their own waiting and declined entries with the fields; once approved, their entry shows without them, like everyone else's.
- **The artist's own screens:** the approve and decline responses, `POST …/pieces`, and `/me/piece-requests` always carry them.

Everyone else sees approved pieces only, without the fields.

**Adding pieces:** `POST /api/events/:id/pieces` with `{pieceId, mode?, priceCents?, deliveryMode?, sortOrder?}` returns `201` with the lineup entry. The host and accepted co-hosts can add anyone's piece; an accepted featured artist can add their own. `DELETE /api/events/:id/pieces/:pieceId` returns `{removed: true}`; the host and co-hosts can take anything off, and an artist can withdraw their own. Anyone else gets `404`.

| Method | Path | Body | Result |
|---|---|---|---|
| POST | `/api/events/:id/pieces/:pieceId/approve` | optional `{"priceCents"}` (the artist's own price) | `200` with the lineup entry. For `sale` or `bid`, this **ends the piece's own listing or auction** (bidders are told it was withdrawn). On a published event the piece is listed for the event at once |
| POST | `/api/events/:id/pieces/:pieceId/decline` | — | `200` |
| GET | `/api/user/me/piece-requests` | — | `{requests: [<lineup entry> + preview: {endsAuction, endsFixedListing, activeBidCount}, event: <card>]}` |

- Only the piece's artist can answer; anyone else gets `404` "Piece request not found."
- Approve needs finished onboarding (`403` "Please complete onboarding before performing this action." otherwise, or the email-not-verified `403`); decline doesn't.

| Status | Message |
|---|---|
| `409` | "You've already answered this request." |
| `409` | "This event is no longer being edited." (cancelled or archived) |
| `409` | "This piece already has a sale in progress and can't be listed again." (approving a `sale` or `bid` entry) |
| `400` | "priceCents must be a whole number." |
| `400` | "A piece being sold needs a price of at least $1.00." |

**Rule (spec):** an event either sells pieces or auctions them, never both. Adding a `sale` piece to an event with `bid` pieces (or the other way round) gets `409` "An event either sells its pieces or auctions them, not both."

**Notifications:**

| Type | Who | Payload |
|---|---|---|
| `event_piece_request` | The artist (also by email) | Title "Piece request". `{pieceId, mode, priceCents, deliveryMode, endsAuction, endsFixedListing, activeBidCount}`, enough to show "approving ends your auction" |
| `event_piece_response` | The host | Title the event's name, body `Mira approved "Paper Planes III".` (or `declined`). `{pieceId, approved}` |

Both target the event.

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

- **Limits:** it must be after the start and at least 30 minutes before the end, otherwise `400` "Bidding has to end after the event starts." or "Bidding has to end at least 30 minutes before the event ends."
- **Published event:** running auctions move to the new end, unless someone has bid: `409` "Bidding has started, so its end time can't change."
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
- `date` is the event's start (ISO, with offset). `eventId` is `null` once the event is deleted.
- `month` is worked out in the event's own time zone, so a late-evening show on the 30th isn't filed under the next month. If the event has since been deleted or has no zone, it falls back to UTC. Show `month` as it is.

**App changes:**
- [ ] Show "Exhibited" on the piece page: event name, venue and month (spec).

### 5.5 Reminders

Ticket holders now get the spec's **two** reminders:
- the day before;
- **2 hours before**, which is new.

Both are `event_reminder` (title "Event reminder", target the event, `payload: {eventTitle, startsAt}`) and both also go by email. The day-before reminder covers 2 to 24 hours before; the 2-hour one covers 10 minutes to 2 hours. A person never gets both at once. After a reschedule, both go out again for the new time.

### 5.6 Time zone from the location (B28)

When an event has `latitude` and `longitude`, its `timezone` is worked out from them on create and on every location change. This is an offline lookup. The `timezone` the phone sends is used only when there's no location, on create and on `PATCH` alike: a `PATCH` with `timezone` on an event that has (or is given, in the same body) a location keeps the location's zone.

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

| `kind` | Meaning | `message` |
|---|---|---|
| `running_auction` | One of their auctions has bids | "An auction of yours has bids. Wait for it to close." |
| `bidding` | They hold a live bid: the leading bid on a running auction, or a bid still in line on one being settled | "You're bidding on an auction that hasn't finished." |
| `shipping_sale` | A piece they sold isn't finished: its order is `pending_payment`, `paid`, `shipped`, `awaiting_confirmation` or `disputed` | "A piece you sold is still on its way." |
| `shipping_purchase` | The same, for a piece they bought | "A piece you bought is still on its way." |
| `pending_payout` | Money is still on its way to them (an art payout, or a ticket payout for a finished event) | "Money is still on its way to you." |
| `hosted_event_tickets` | An upcoming event they host has tickets sold | "You're hosting an event with tickets sold. Cancel it first (everyone is refunded)." |
| `fee_debt` | They owe card fees from an event they cancelled. `count` is always `1`; the amount is in `amountCents` | "You owe card fees from an event you cancelled." |

`DELETE /api/user/me` (still with `{"password"}`) checks the password first:
- no password: `400` "Password is required to delete your account.";
- wrong password: `401` "Password is incorrect.".

Then, while the list isn't empty, it refuses with **`409`** and the first blocker's message followed by " (See what's left in Delete account.)".

Listings and auctions without bids no longer block deletion; they're taken down as part of deleting. Past events that had tickets are kept, under the anonymized host, because buyers' tickets and receipts point at them.

**App changes:**
- [ ] `delete_account_page.dart`: read the list from `/me/deletion-check` instead of building it from listings, sales and purchases. Disable Delete while `canDelete` is false, and show each `message`.
- [ ] Confirming still takes the password. B34 asks whether typing DELETE should replace it; that's the client's decision.

### 6.4 Hide from my Collection (B32)

| Method | Path | Result |
|---|---|---|
| POST | `/api/collection/:pieceId/hide` | `200` `{pieceId, hidden: true}`, message "Hidden from your Collection." |
| DELETE | `/api/collection/:pieceId/hide` | `200` `{pieceId, hidden: false}` (undo), message "Back in your Collection." |

- Only a piece the caller collected can be hidden; anything else gets `404` "That piece isn't in your Collection.". A malformed id gets `400` "Invalid piece id.".
- A hidden piece leaves `GET /api/users/:username/collection` for everyone, the owner too.
- The owner can ask for `?includeHidden=1` (or `=true`) to see everything. On the owner's own Collection, each item carries `hidden: bool`.
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
- **Shipping, insurance and tax were paid at `auction-checkout`** after a win in 7a. Phase 7b (after Convelio shipping, Phase 8) now charges them automatically after the close wherever it can, and keeps the checkout only as the fallback.

### 7a.1 Bidding: a card check, not a hold

`POST /api/pieces/:id/bids` keeps its request, plus an optional `addressId` (the design's Ship to row):

```json
{"amountCents": 32500, "paymentMethodId": "pm_…", "addressId": "…"}
```

- **No money is held any more.** The first bid only checks that `paymentMethodId` is a card saved to this account. Save it first with the existing `POST /api/payments/setup-intent` and PaymentSheet, where the bank verifies it.
  - Nothing is charged unless they win.
- Raising your own bid reuses the card and address. Send new ones to change them.
- `addressId` must be one of the bidder's addresses. It's used by Phase 7b.

| Status | Message |
|---|---|
| `400` | "A payment method is required to bid." (first bid with no `paymentMethodId`) |
| `400` | "Save this card first (it's checked once, then only charged if you win)." (a card not saved to this account) |
| `400` | "We couldn't find that card. Add it again and try once more." |
| `400` | "Invalid address id." |
| `404` | "Address not found." (not one of theirs) |
| `409` | "Bid must be at least $X." / "Bidding hasn't opened for this auction yet." / "Bidding has closed for this piece." |

The bidder gets `bid_placed` ("You're currently winning", `payload.amountCents`), and the person they passed gets `outbid`.
- **Increments are the design's.** The step is based on the current high bid:

  | Current high bid | Step |
  |---|---|
  | Under $500 | $25 |
  | $500 to $999 | $50 |
  | $1,000 to $4,999 | $100 |
  | $5,000 to $9,999 | $250 |
  | $10,000 and up | $500 |

  `minNextBidCents` and `bidIncrementCents` in the summary already reflect these.
- **Fixed end time:** no automatic extensions. A late bid no longer pushes the end. The summary carries `fixedEndTime: true`. (Auctions that started before this release keep their old setting.)
- **The seller's one manual extension still exists:** `POST /api/pieces/:id/auction/extend {"extraDays": 1-3}`, owner only. It's allowed once, only more than 3 days before the close, and never on an event auction. `200` returns the summary, and every bidder gets `auction_extended` ("The seller extended "…" by 2 days. Your bid still stands."). Refusals: `400` "extraDays is required." / "An auction can be extended by up to 3 days."; `409` "This piece has no auction running.", "Only a running auction can be extended.", "An event auction's timing comes from the event.", "This auction has already been extended once.", "An auction can only be extended more than 3 days before it closes."
- The outbid notification now says "Nothing is charged unless you win", not "your hold still stands". No server notification mentions a hold any more, except `bid_hold_expired` for auctions from before this release (2A.4).

**App changes:**
- [ ] First bid (`NoteBidVerify`): use PaymentSheet with Apple Pay through a SetupIntent, and add the Ship to address row. Send `paymentMethodId` and `addressId`.
- [ ] Remove any "hold" wording.
- [ ] Use the server's `minNextBidCents`. Quick picks step by the table above.
- [ ] Don't show "extends if someone bids late".

### 7a.2 When the auction ends: the saved card is charged

At the close, the winner's saved card is charged **off-session** for the hammer price.
- The winner gets `auction_won` (`payload.amountCents`): "You won "…" for $X, and your card has been charged." followed by the next step:
  - "Confirm your delivery details to finish." when they check out themselves (7b `manual`);
  - "We'll charge shipping, insurance and tax to the same card and book the courier — nothing more to do." when Phase 7b takes over.
- The seller gets `auction_sold` (`payload.amountCents`). Every other bidder gets `auction_lost` ("Nothing was charged to you").
- The auction goes to `awaiting_winner`, the same state the app already handles for checkout.

**No sale:**
- No bids: the seller gets `auction_ended_no_bids`; the auction is `closed_no_bids`.
- Under the reserve: the seller gets `auction_reserve_not_met`; the auction is `needs_seller_action`. Every bidder gets `auction_no_sale` ("Auction ended without a sale", "Nothing was charged to you.").

### 7a.3 Card fails: 12 hours to fix it (`NoteBidPaymentFailed`)

If the charge fails (a bank verification request counts as a failure, because nobody is there to answer it):
- The auction goes to `awaiting_payment`.
- The winner gets `auction_payment_failed` with `payload: {amountCents, deadlineAt}`, and the seller gets `auction_winner_payment_failed` with the same payload.
- `autoCheckout` (7b) is `null` in this state.

The winner then has **12 hours**. The summary shows them:
- `awaitingPayment: true`;
- `winnerDeadlineAt`, the end of the window, which **never moves** however many cards are tried (BE-4);
- `paymentRetryAt`, the next automatic retry (at 1, 4 and 8 hours), or `null`.

`POST /api/pieces/:id/auction/retry-payment` with `{"paymentMethodId"}` charges a new card straight away (the card is checked as in 7a.1 first):

| Status | Meaning |
|---|---|
| `200` | Paid. Returns the auction summary (now `awaiting_winner`). The winner gets `auction_payment_fixed` ("Payment received", `payload.amountCents`, body ending with the same next step as `auction_won`), and the seller gets `auction_sold` |
| `402` | "That card was declined too. Try another one before your time runs out." They can try another until the deadline |
| `400` | "paymentMethodId is required." |
| `403` | "Only the winning bidder can do this." |
| `409` | "There's no payment waiting to be fixed on this piece." |

**App changes:**
- [ ] Payment-failed screen: a countdown to `winnerDeadlineAt`, "We'll try again at `paymentRetryAt`", and "Use another card", which saves a card and then calls `retry-payment`.

### 7a.4 Second chance (`NoteBidSecondChance`)

When the 12 hours run out, the winner forfeits (notification `auction_forfeited`, nothing charged). The **next bidder whose top bid met the reserve** is offered the piece at that bid:

- The auction status becomes **`second_chance`**, which is new.
- The bidder gets `auction_second_chance` (also by email), with `payload: {amountCents, expiresAt, offerId}`. The seller gets `auction_second_chance_sent`.
- The offered bidder's summary carries `secondChanceOffer: {amountCents, expiresAt, lastError}`. It's `null` for everyone else.

| Method | Path | Body | Result |
|---|---|---|---|
| POST | `/api/pieces/:id/auction/offer/accept` | optional `{"paymentMethodId"}` | Charges their saved card (or this one) at once. `200` with the summary: they're the winner (`isWinner`, `awaiting_winner`); they get `auction_won` and the seller `auction_sold`. `402` "<the card's error> Try another card before the offer runs out." if declined: the offer stays open until it expires, and `lastError` says why |
| POST | `/api/pieces/:id/auction/offer/decline` | — | `200` `{declined: true}`. The offer passes to the next bidder |

Both refuse with `404` "There's no offer waiting for you on this piece." (no offer, or it's someone else's) and `409` "This offer has expired.".

- While they hold an offer, the bidder's `myBidStatus` reads `outbid` (Reference).
- An offer lasts **24 hours**. Unanswered, it passes on, and that bidder gets `auction_second_chance_expired`.
- The cascade stops after 3 attempts in all. If nobody is left, the next bid is under the reserve, or the 3 attempts are used up, the auction goes to `needs_seller_action`. The seller gets `auction_needs_action`, and the piece comes off sale, as before.
- Nobody is ever charged without saying yes. The old automatic charging of the runner-up is gone, except for auctions still on holds from before this release.

**App changes:**
- [ ] The Second chance screen: the amount, a countdown to `expiresAt`, and "Accept and pay" (call accept, then open the collected screen). Handle `402` with "Use another card".
- [ ] Handle `auction_second_chance` and `auction_second_chance_expired`: open the piece.
- [ ] Treat the `second_chance` status as "not available" on the piece page for everyone else.

### 7a.5 Abandoned auction checkout (PAY-3)

`POST /api/pieces/:id/auction-checkout` no longer marks the auction sold. It becomes `closed_sold` only when the checkout order is paid.
- `201` returns the order plus `balanceDueCents` (what the PaymentSheet charges: shipping, insurance and tax) and `prepaidCents` (the hammer price already charged).
- `addressId` is required (`400` "addressId is required."; `404` "Address not found.").
- `409` "Your payment for this piece hasn't gone through yet. Update your card first." while the auction is `awaiting_payment`.
- `409` "This auction hasn't ended yet or has already been claimed." when the piece isn't `auction_won` (for example, an order is already open on it).
- `403` "Only the winning bidder can complete this purchase."

If the winner abandons the checkout (the 15-minute sweep cancels the order), the piece goes back to them as `auction_won`, and they can check out again. A winner never times out once the hammer price is charged: `awaiting_winner` has no deadline. Before this fix it was delisted and their hammer price was stuck.

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

Built to the Convelio plan and Convelio's v2 API. **Every shipment goes through Convelio** wherever the server has a Convelio key (`CONVELIO_ENABLED` defaults to on). The flat rates remain only as a fallback, for a server without a key or with `CONVELIO_ENABLED=false`. **Until the new app ships, an installed app can't check out on a server that has a Convelio key**: it sends `shippingMethod`, not a quote, and gets `400`.
- **With Convelio on**, checkout needs a `quoteId`.
- **Checking which mode is on:** `GET /api/pieces/:id/shipping-quote` returns `live: true` or `false`.
  - Convelio on: `{methods: [], live: true, speeds: ["regular_speed", "express"], deliveryTypes: ["curbside", "white_glove"], insuranceDefault: true}`.
  - Convelio off: `{methods: [{id, cents}], live: false}`, the flat tiers as before.

### 8.1 What the artist fills in (listing, `SettingsSelling`)

Convelio prices on the **packed** piece. Before a piece can be shipped, the piece and the artist need:

| Field | Notes |
|---|---|
| `packageLengthCm`, `packageWidthCm`, `packageHeightCm` | Packed size. Collect it in inches and convert |
| `weightKg` | Packed weight. Collect it in pounds and convert |
| `declaredValueCents` | Optional; the price is used otherwise |
| `shippingItemType` | One of `fine_art.painting`, `fine_art.picture`, `fine_art.sculpture`, `fine_art.installation`, `decorative.ceramics`, `decorative.artefact`, `other.other`. If not sent, it's guessed from the medium |
| `shippingPacking` | `not_packed` (Convelio crates it, the default) or `wood_crated` |
| `shippingMaterials` | A list from `glass`, `stone`, `ceramic`, `plaster_concrete`, `resin`, `none`, `ornate_frame`, `framed_glass`, `unframed`, `lacquered`, `stackable`, `dismountable`, `can_be_rolled`, `can_be_folded`, `horizontal`, `pastel`, `protruding`, `wet`, `rolled_in_plastic` |
| **Pickup address** (on the artist, B31) | `PUT /api/user/me/pickup-address {"addressId"}`, one of their own **US** addresses **with a phone** (Convelio calls it to arrange pickup). `GET` returns it. Only ever shown to the artist |

**Pickup address endpoints:** `GET` and `PUT /api/user/me/pickup-address` both return `{pickupAddress: {id, firstName, lastName, phone, line1, line2, city, state, zip, country} | null}`. `PUT` with `addressId` `null` or `""` clears it.

| Status | Message |
|---|---|
| `404` | "Address not found." |
| `400` | "Pickup addresses are US only for now." |
| `400` | "Add a phone number to this address: the courier calls it to arrange pickup." |

The piece's owner sees `shippingReady: {ready, missing: [...]}` on the piece detail. The `missing` labels are "packed length", "packed width", "packed height", "weight", "value" and "pickup address".

**Listing for sale now requires these when Convelio is on** (the default wherever a key is set). Creating a piece with `isForSale: true`, or turning sale on with `PATCH`, is refused with `400` and a message naming what's missing:
- `packageLengthCm`, `packageWidthCm`, `packageHeightCm` and `weightKg`;
- the artist's pickup address.

For example: "To sell this piece, add its package weight, package length, pickup address (Settings, Selling). The courier prices shipping on them."

- A piece that isn't for sale needs none of this.
- Any later edit to an already-listed piece's price or listing terms runs the same check.
- Listing for sale also needs `medium` ("Medium is required for sale listings.") and `dimensions` ("Dimensions are required for sale listings.").
- Packed size and weight, when sent, must be positive numbers: `400` "Package weight must be greater than zero." / "Package weight must be a number." (same for length, width, height). `declaredValueCents`: "Declared value must be greater than zero." / "Declared value must be a number."
- `shippingItemType`, `shippingPacking` and `shippingMaterials` are accepted on create and `PATCH`, and returned on the piece. Each is checked against Convelio's lists:
  - `400` "shippingItemType must be one of: …";
  - `400` "shippingPacking must be not_packed or wood_crated.";
  - `400` "shippingMaterials must be a list from: …".

**App changes:**
- [ ] Listing form: packed size in inches, weight in pounds, packing, item type, and materials or framing. **Required to list for sale while Convelio is on.** Show the `400` message, and send the artist to the Pickup address screen when it names it.
- [ ] A Pickup address screen in Settings, Selling.
- [ ] Show `shippingReady.missing` to the artist.

### 8.2 The piece page estimate (B19)

`GET /api/pieces/:id/shipping-estimate?zip=&state=` (no sign-in needed):

```json
{"available": true, "source": "table", "lowCents": 25000, "highCents": 60000, "currency": "USD", "live": false}
```

- `source: "convelio"` when Convelio prices it; for now it doesn't for US routes, so `source` is `"table"`, a range from the packed size and weight.
- `available: false` when the artist hasn't added the size and weight. That response is `{available: false, missing, reason, live}`, with no `source`, `lowCents` or `highCents`.

**App changes:**
- [ ] "Shipping from about $250 to $600" on the piece page.

### 8.3 Checkout with a live quote (`collect_piece_sheet.dart`)

**1. Quote.** `POST /api/pieces/:id/shipping-quotes`:

```json
{"addressId": "…", "speed": "regular_speed" | "express", "deliveryType": "curbside" | "white_glove", "insurance": true}
```

`speed` defaults to `regular_speed`, `deliveryType` to `curbside`, and `insurance` to on. The response is `201`:

```json
{"id": "…", "status": "created", "pending": false, "speed": "regular_speed", "deliveryType": "curbside",
 "insurance": true, "shippingCents": 34000, "insuranceCents": 2500, "totalShippingCents": 36500,
 "currency": "USD", "reference": "QVO-…", "expiresAt": "…", "createdAt": "…"}
```

- The call takes **2 to 9 seconds**, and the server itself waits up to 30 seconds for Convelio plus 10 to connect. Show a loading state and use a client timeout of about **45 seconds**.
- **`pending: true`** (`status: "processing"`) means Convelio is pricing it by hand, within 24 hours. The prices are `null`. Show "Shipping quote on its way: we'll notify you".
  - Nothing is reserved. The buyer gets `shipping_quote_ready` (title "Shipping quote ready", target the piece, `payload: {quoteId, expiresAt}`).
  - `GET /api/shipping-quotes/:id` then shows `status: "ready"` with prices. It reads what the server has stored and never asks Convelio again, so poll it only after the notification. Someone else's quote is `404` "Shipping quote not found."
- Quote statuses: `created` (instant price), `processing` (pending), `ready` (priced by hand), `expired`, `used` (an order was placed on it) and `failed`.

| Status | Message | Fix |
|---|---|---|
| `409` | "The artist needs to add this piece's packed length, weight, … before it can ship." | Not the buyer's to fix: show it, and offer to message the artist |
| `409` | "The buyer's contact details need: phone." (or first name, last name, email) | **The buyer's address needs a first name, last name and phone, and the account an email.** Let them edit the address and quote again |
| `409` | "Live shipping quotes aren't switched on yet." | Convelio is off on this server; use the flat tiers |
| `400` | "Shipping is US only for now." | Non-US address |
| `400` | "speed must be one of: regular_speed, express." / "deliveryType must be one of: curbside, white_glove." | |
| `400` | "This is your own piece." | |
| `404` | "Address not found." | Not one of the buyer's addresses |
| `422` | "Shipping couldn't be priced: …" | Convelio couldn't price this route or piece |
| `400` | "Shipping request was refused: …" | Convelio refused the request |
| `502` | "The shipping service isn't answering right now. Please try again." | Retry |
| `503` | "Shipping isn't set up yet." | No Convelio key on this server |

- The buyer's phone must read as a real number (10 US digits, or `+` and 8 to 15 digits); otherwise it counts as missing. Make phone required on the address form used for checkout.
- `shipping_quote_ready` isn't sent for an auction win's automatic quote; 7b has its own notifications.

**2. Order.** `POST /api/pieces/:id/collect` with `{"addressId", "quoteId"}`. The server takes the price from the quote; the app never sends one.

| Case | Status and message |
|---|---|
| The quote is still pending | `409` "This shipping quote is still being priced. We'll let you know when it's ready." |
| The quote has expired | `409` "This shipping quote has expired. Get a new one." (expires after 24 hours for now) |
| The quote was used or failed | `409` "This shipping quote can't be used any more. Get a new one." (a quote is `used` once its order's courier is booked) |
| The quote is for another address | `409` "That shipping quote is for a different address." |
| Convelio on, no `quoteId` | `400` "Get a shipping quote for this address first (quoteId)." |
| `quoteId` isn't an id | `400` "Unknown shipping quote." |
| Not this buyer's quote for this piece | `404` "Shipping quote not found." |
| No `addressId` | `400` "addressId is required." |

The order carries:
- `shippingCents`: shipping plus insurance;
- `insuranceCents`: the insurance part of that;
- `deliveryType`;
- `taxCents`;
- `shippingMethod`: the quote's speed (`regular_speed` or `express`), or `pickup`.

- **A piece collected in the room at an event:** send `"shippingMethod": "pickup"` instead of a quote. `addressId` is still required (it's the billing and tax address). Only allowed for a piece on a published event with pickup delivery; otherwise `400` "This piece isn't collected at an event; choose shipping."
- **Auction checkout** (`/auction-checkout`) takes the same `quoteId` (or `pickup`), with the same errors.

**3. Payment.** As now. After the payment goes through, Studio 3 books the courier with Convelio. Nothing is booked before payment.

**App changes:**
- [ ] Replace the 4 flat tiers with Regular or Express, Curbside or White-glove, and an insurance toggle.
- [ ] Show the quote's shipping and insurance on separate lines.
- [ ] Add loading, error and pending states.
- [ ] When the quote names the buyer's contact details, open the address editor so they can add the phone (or name), then quote again.
- [ ] Handle the `shipping_quote_ready` notification by opening checkout again on that quote.

### 8.4 Tracking (B32)

Order responses and the piece's `viewerOrder` carry the shipment:

```json
"shipment": {"provider": "convelio", "providerStatus": "out_for_delivery", "status": "in_transit",
  "courier": "Convelio", "trackingNumber": "CVO-…", "shipmentDate": null,
  "trackingUrl": "https://…", "reference": "CVO-…", "deliveredAt": null, "onHold": false, "cancelled": false,
  "steps": [{"key": "booked", "done": true}, {"key": "picked_up", "done": true},
            {"key": "in_transit", "done": true}, {"key": "out_for_delivery", "done": true},
            {"key": "delivered", "done": false}]}
```

- **Fields:** `trackingNumber` falls back to the Convelio reference. `status` is the simple state: `label_created`, `picked_up`, `in_transit` or `delivered`. `providerStatus` is Convelio's own (for example `packing_in_progress`, `freight_in_transit`, `out_for_delivery`, `on_hold`, `canceled`).
- **On a collected piece:** `GET /api/pieces/:id` adds `viewerOrder: {id, side: "buyer" | "seller", status, deliveryType, shipment, pickupAt, arrivalWindow, onTime}` for the buyer and the artist; `null` for everyone else. It's the latest order on the piece that wasn't `cancelled` or `failed`, so a refunded order still shows (as `refunded`).
- **Not available yet:** `pickupAt`, `arrivalWindow` and `onTime` stay `null`. Convelio arranges pickup with the artist by phone and gives no delivery estimate in its API.
- **The order moves by itself:**
  - Convelio's `shipment_created`, `picked_up`, `packing_in_progress`, `freight_in_transit` or `out_for_delivery` move a `paid` order to `shipped` (export and import updates don't);
  - `shipment_completed` (delivered) → `awaiting_confirmation` (through `shipped` if needed), and the buyer gets `order_delivered`.
- **Buyer notifications** (all target the order):

  | Type | When | Title | Payload |
  |---|---|---|---|
  | `shipment_picked_up` | The courier collected it | "Picked up" | `{side: "buyer", status}` |
  | `shipment_out_for_delivery` | It's out for delivery | "Out for delivery" | `{side: "buyer", status}` |
  | `order_delivered` | Delivered | "Your artwork has arrived" | `{side: "buyer"}` |
  | `shipment_on_hold` | Convelio put it on hold | "Shipment on hold" | `{side: "buyer", status}` |
  | `shipment_canceled` | The shipment was cancelled | "Shipment cancelled" | `{side: "buyer", status}` |

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
1. **At the close**, in the same commit that settles the auction on its winner, the server decides whether 7b applies and sets `autoCheckout.state`: `manual` if it doesn't (see "Not used" below), otherwise `pending_quote`.
2. Convelio is asked for a quote to the winner's **Ship to** address, the `addressId` sent with their first bid (7a.1). The quote is always Regular speed, curbside, with the default insurance; the winner doesn't choose.
3. **Instant price:** the winner's order is created and shipping, insurance and tax are charged at once. The courier is booked exactly as for any sale (8.3, 8.4).
4. **Custom quote** (Convelio prices it by hand within 24 hours): the winner gets `auction_shipping_pending` ("Shipping price on its way"). The state stays `pending_quote`, and the charge happens automatically when Convelio's price arrives; no checkout to come back to. There is **no timeout**: only Convelio's webhook moves it on.
5. **That charge fails** (declined, or no saved card): the order is called off and the piece goes back to the winner. The state becomes `failed`; they get `auction_shipping_failed` ("Finish your purchase") and complete at the existing `POST /api/pieces/:id/auction-checkout` with another card.

When it's done the winner gets `auction_shipping_charged` ("Delivery booked"), and the piece shows its `viewerOrder` with tracking (8.4). All three 7b notifications target the piece, with `payload: {auctionId}`.

The win message says which path they're on. `auction_won` (and `auction_payment_fixed`) ends with "Confirm your delivery details to finish." for `manual`, or "We'll charge shipping, insurance and tax to the same card and book the courier — nothing more to do." otherwise.

**Not used** (the state is `manual` from the close, and the winner checks out as before, at `auction-checkout`) when:
- Convelio is off;
- the piece is collected at the event (`deliveryMode: "pickup"`);
- the winning bid has no Ship to address;
- the artist hasn't added the packed size, weight or pickup address;
- the Ship to or pickup address is outside the US;
- a name, email or usable phone is missing on either address (8.3).

All of these are checked at the close, so the win message matches. Afterwards only Convelio itself can still send it to `manual`: it refuses or fails to quote, or the quote can't be used. That's rare, and the win message will then have said "nothing more to do". So go by `autoCheckout.state`, not the message text.

**While the auction is settling,** the winner's auction summary carries:

```json
"autoCheckout": {"state": "pending_quote" | "charged" | "failed" | "manual", "orderId": "…" | null}
```

- It's `null` for everyone else.
- It's also `null` for the winner while the auction is `awaiting_payment` (7a.3) or `second_chance` (7a.4), and for auctions settled before this release. A winner who reaches `awaiting_winner` by fixing their card or accepting an offer gets a state at that point.
- `orderId` is set while the automatic order is being charged.

| `state` | What to show |
|---|---|
| `pending_quote` | "Shipping price on its way". Don't open checkout |
| `failed` | "Finish your purchase", opening the auction checkout |
| `manual` | The auction checkout, as today |
| `charged` | Effectively never seen (below) |

**On success the auction fields disappear.** Paying the order marks the auction `closed_sold` straight away, and the piece detail only summarises running or settling auctions. So the whole auction summary, `autoCheckout` included, is gone from `GET /api/pieces/:id` by the time the app reads it. Read the piece's `viewerOrder` (8.4) instead: when it's there, the purchase is done.

**App changes:**
- [ ] After winning, if the piece has a `viewerOrder`, show the order and tracking. Otherwise read `autoCheckout.state`:
  - `pending_quote`: show the shipping-pending note; don't open checkout.
  - `failed` or `manual`: open the auction checkout.
  - `null` while `awaiting_winner`: only for auctions settled before this release; treat as `manual`.
- [ ] Handle the `auction_shipping_pending`, `auction_shipping_charged` and `auction_shipping_failed` notifications: open the piece.
- [ ] Make sure the first bid sends `addressId` (7a.1). Without it, the winner falls back to checkout.

**Server-side only (for awareness):**
- **Deploy:** migration `061_auction_auto_checkout`. No new settings: it runs wherever Convelio is on (the default when a key is set).
- **Same payment path:** the follow-on charge is an ordinary order payment (metadata `kind: auction_balance`), so refunds, payouts and the ledger treat it like any sale.

---

## Reference

### Notifications

Every notification the backend sends that the app can receive. Each one is an inbox row (`GET /api/notifications`) and a push, unless the person turned that `type` off in Settings. The stored `payload` always also carries `message` (the body text); the table lists the other keys. A tap should open `targetType` / `targetId`.

**Art orders and shipping**

| `type` | Recipient | `targetType` | Payload keys | Meaning |
|---|---|---|---|---|
| `purchase` | Buyer | `order` | `pieceTitle` | "Order confirmed": their order was paid |
| `sale` | Artist | `order` | `pieceTitle`, `totalCents` | "You made a sale!" |
| `order` | Buyer | `order` | — | "Order cancelled" (artist cancelled a paid order, refunded) or "Refund issued" (admin refund) (2.2) |
| `order` | Artist | `order` | — | "Payment released": an admin resolved a dispute in their favour |
| `order_shipped` | Buyer and artist | `order` | `pieceTitle`, `courier`, `trackingNumber`, `side` | "Order shipped", when Studio 3 enters a shipment by hand (not Convelio) |
| `order_delivered` | Buyer | `order` | `side` (+ `pieceTitle` when entered by hand) | "Your artwork has arrived": confirm receipt to release payment (8.4) |
| `shipment_picked_up` | Buyer | `order` | `side`, `status` | The courier collected it |
| `shipment_out_for_delivery` | Buyer | `order` | `side`, `status` | Out for delivery |
| `shipment_on_hold` | Buyer | `order` | `side`, `status` | Convelio put it on hold |
| `shipment_canceled` | Buyer | `order` | `side`, `status` | The shipment was cancelled |
| `shipping_quote_ready` | Buyer | `piece` | `quoteId`, `expiresAt` | A pending quote is priced; reopen checkout on it (8.3) |

**Auctions**

| `type` | Recipient | `targetType` | Payload keys | Meaning |
|---|---|---|---|---|
| `bid_placed` | Bidder | `piece` | `amountCents` | "You're currently winning" |
| `outbid` | The bidder who was passed | `piece` | `amountCents` | "You've been outbid. Nothing is charged unless you win." |
| `auction_extended` | Every bidder | `piece` | — | The seller used their one extension. "Your bid still stands." |
| `auction_cancelled` | Every bidder | `piece` | `amountCents`, `reason` | The auction was withdrawn (by the seller, an event, or the piece being tagged to an event). Nothing charged |
| `auction_won` | Winner | `piece` | `amountCents` | Won and charged; the body ends with the next step (7a.2, 7b) |
| `auction_sold` | Seller | `piece` | `amountCents` | Their auction sold (at close, after a card fix, or on an accepted offer) |
| `auction_lost` | Other bidders at the close | `piece` | — | Went to another bidder; nothing charged |
| `auction_no_sale` | Every bidder | `piece` | — | Ended below the reserve. "Nothing was charged to you." |
| `auction_ended_no_bids` | Seller | `piece` | — | Ended with no bids |
| `auction_reserve_not_met` | Seller | `piece` | — | Ended below the reserve; decide what to do next |
| `auction_payment_failed` | Winner | `piece` | `amountCents`, `deadlineAt` | Card declined at the close; 12 hours to fix (7a.3) |
| `auction_winner_payment_failed` | Seller | `piece` | `amountCents`, `deadlineAt` | The winner's payment failed |
| `auction_payment_fixed` | Winner | `piece` | `amountCents` | "Payment received" after `retry-payment`; body ends with the next step |
| `auction_forfeited` | The winner who didn't pay | `piece` | — | Their window ran out; the piece went to another bidder |
| `auction_second_chance` | Next bidder (also email) | `piece` | `amountCents`, `expiresAt`, `offerId` | Offered the piece at their top bid (7a.4) |
| `auction_second_chance_sent` | Seller | `piece` | — | The piece was offered to the next bidder |
| `auction_second_chance_expired` | Bidder whose offer lapsed | `piece` | — | Their offer passed on |
| `auction_needs_action` | Seller | `piece` | — | Nobody could pay; relist or set a fixed price |
| `auction_shipping_pending` | Winner | `piece` | `auctionId` | 7b: the shipping price is on its way |
| `auction_shipping_charged` | Winner | `piece` | `auctionId` | 7b: "Delivery booked" |
| `auction_shipping_failed` | Winner | `piece` | `auctionId` | 7b: "Finish your purchase" at the auction checkout |
| `bid_hold_expired` | Bidder (pre-7a auctions only) | `piece` | `amountCents`, `bidId` | Their card hold couldn't be renewed (2A.4) |

**Events and tickets**

| `type` | Recipient | `targetType` | Payload keys | Meaning |
|---|---|---|---|---|
| `event_reminder` | Ticket holders (also email) | `event` | `eventTitle`, `startsAt` | The day before, and 2 hours before (5.5) |
| `event_rescheduled` | Ticket holders (also email) | `event` | `startsAt` | The event moved (4.4) |
| `event_cancelled` | Guests and the waitlist (also email) | `event` | — | Cancelled (4.5), or removed because it or its host was deleted |
| `ticket_refunded` | Buyer | `event` | `orderId`, `amountCents` | "Refund issued" for a buyer or admin refund (4.1) |
| `waitlist_hold` | The person next in line (also email) | `event` | `heldUntil` | Tickets are held for them (4.2) |
| `waitlist_hold_ended` | The person whose hold ran out | `event` | — | Their hold passed on |
| `event_tickets_available` | People who saved the event | `event` | — | A sold-out event has tickets again |
| `event_invite` | Invitee (also email) | `event` | `role`, `eventTitle` | Invited as co-host or featured artist (5.1) |
| `event_invite_response` | Host | `event` | `accepted`, `roles` | The invitee answered |
| `event_piece_request` | Artist (also email) | `event` | `pieceId`, `mode`, `priceCents`, `deliveryMode`, `endsAuction`, `endsFixedListing`, `activeBidCount` | A host tagged their piece (5.2) |
| `event_piece_response` | Host | `event` | `pieceId`, `approved` | The artist approved or declined |

**Social, saves and messages**

| `type` | Recipient | `targetType` | Payload keys | Meaning |
|---|---|---|---|---|
| `follow` | The person followed, or the follower whose request was accepted | `user` | `followerUsername`, `followerName`; or `accepted: true` | New follower, or "Your follow request was accepted" (also when an account goes public, 6.1) |
| `follow_request` | A private account | `user` | `followerUsername`, `followerName` | Someone asked to follow them |
| `like` | The owner | `piece` or `post` | `likerUsername`, `likerName` | Someone liked their piece or post |
| `save` | The owner | `piece` or `post` | `saverUsername`, `saverName` | Someone saved it |
| `comment` | The owner | `piece` or `post` | `commentPreview`, `commenterUsername` | A new comment |
| `inquiry` | The other side of the thread | `inquiry` | `message` (+ `pieceTitle` on a new thread) | A new inquiry or reply about a piece |
| `saved_piece_listed` | People who saved the piece | `piece` | `priceCents` | A saved piece went on sale |
| `saved_piece_price` | People who saved the piece | `piece` | `priceCents`, `wasPriceCents` | Its price changed |
| `saved_piece_auction` | People who saved the piece | `piece` | `priceCents`, `biddingOpen`, `opensAt?` | It's going to auction |
| `followed_artist_post` | Followers | `piece` or `post` | `pieces`, `scenes` | An artist they follow posted (one row a day, updated) |
| `message` | Chat recipient | — | push data `{type, conversationId}` | Push only: no inbox row. Opens the chat |

`system` notifications go to Studio 3 admins only (disputes, reports); the app can ignore them for other users.

### Status enums

**Ticket order** (`GET /api/events/:id/orders/:orderId` `status`)

| Value | Meaning |
|---|---|
| `pending_payment` | Checkout started; tickets held for 10 minutes |
| `paid` | Tickets issued |
| `partly_refunded` | Some tickets refunded; the rest still work |
| `refunded` | Every ticket refunded |
| `expired` | The hold ran out unpaid |
| `failed` | The payment failed or couldn't start |
| `cancelled` | The event was cancelled or ended while paying; any money is refunded |

**Ticket** (`status`)

| Value | Meaning |
|---|---|
| `valid` | Admits someone |
| `checked_in` | Already scanned |
| `refunded` | Refunded (seen only in a scan result) |
| `void` | Given back with Not going, or the event was cancelled (seen only in a scan result) |

`/me/tickets`, the guest list and the manifest only ever list `valid` and `checked_in`.

**Auction** (`shared/models/auction.py`)

| Value | Meaning |
|---|---|
| `draft` | Created, not open yet (including an event auction before the doors) |
| `live` | Taking bids |
| `closing` | Taking bids, inside the old soft-close window (pre-7a auctions only) |
| `awaiting_payment` | Closed; the winner's card failed and they have 12 hours (7a.3) |
| `awaiting_winner` | Closed and the hammer price charged; delivery outstanding (7b, or the auction checkout) |
| `second_chance` | Offered to the next bidder (7a.4) |
| `closed_sold` | Sold and the follow-on order paid. The piece no longer shows an auction summary |
| `closed_no_bids` | Ended with no bids |
| `closed_reserve_not_met` | Ended below the reserve (older auctions; new ones park as `needs_seller_action`) |
| `needs_seller_action` | No sale; the seller decides (relist, or a fixed price) |
| `cancelled` | Withdrawn |

The piece's auction summary is present for `draft`, `live`, `closing`, `awaiting_payment`, `awaiting_winner` and `second_chance` only.

**`myBidStatus`** (piece detail, signed-in viewers)

| Value | Meaning |
|---|---|
| `leading` | Bidding is open and theirs is the highest bid |
| `outbid` | Bidding is open and someone is higher; or it closed and they're a runner-up still in line (including while they hold a second-chance offer) |
| `won` | Their bid won (paid or still being paid) |
| `lost` | It closed and they didn't get it (someone else won, below the reserve, cancelled, or they won and didn't pay) |
| `none` | No bid in this run |

**`autoCheckout.state`** (7b, winner only)

| Value | Meaning |
|---|---|
| `pending_quote` | Getting Convelio's price, or waiting for a custom quote. No timeout |
| `charged` | Shipping, insurance and tax charged. In practice the auction is already `closed_sold` and the summary is gone; read `viewerOrder` |
| `failed` | The follow-on charge failed; finish at `auction-checkout` |
| `manual` | 7b doesn't apply; finish at `auction-checkout` |
| `null` | Not the winner, `awaiting_payment` / `second_chance`, or an auction settled before 7b |

**Shipping quote** (`status`)

| Value | Meaning |
|---|---|
| `created` | Instant price, ready to buy |
| `processing` | Custom quote; Convelio prices it within 24 hours (`pending: true`) |
| `ready` | A custom quote that has been priced, ready to buy |
| `expired` | Past `expiresAt`; get a new one |
| `used` | An order was placed and booked on it |
| `failed` | Can't be used |

**Shipment** (`shipment.status`; `providerStatus` is Convelio's raw value)

| Value | Meaning |
|---|---|
| `label_created` | Booked (`shipment_created`) |
| `picked_up` | The courier collected it |
| `in_transit` | `packing_in_progress`, `export_in_progress`, `freight_in_transit`, `import_in_progress` or `out_for_delivery` |
| `delivered` | `shipment_completed` |

`providerStatus` can also be `on_hold` or `canceled` (see `onHold` and `cancelled`). The tracker `steps` keys are `booked`, `picked_up`, `in_transit`, `out_for_delivery`, `delivered`.

**Art order** (`GET /api/orders/:id` `status`)

| Value | Meaning | Can move to |
|---|---|---|
| `pending_payment` | Checkout started | `paid`, `failed`, `cancelled` |
| `paid` | Paid; waiting for the courier | `shipped`, `cancelled`, `refunded`, `disputed` |
| `shipped` | On its way | `awaiting_confirmation`, `disputed`, `refunded` |
| `awaiting_confirmation` | Delivered; waiting for "Confirm received" (or 7 days) | `completed`, `disputed`, `refunded` |
| `completed` | Done; the artist is paid | `refunded`, `disputed` |
| `disputed` | The buyer reported an issue; Studio 3 is looking | `completed`, `refunded` |
| `cancelled` | Called off before payment | — |
| `failed` | Payment failed | — |
| `refunded` | Refunded in full (an artist cancel shows this, not `cancelled`) | — |

### Where to start

A suggested build order, each step usable on its own:

1. **Phase 1, 2, 2A:** error messages on the sign-in and code screens; order detail buttons (cancel, Report an issue, `refunded`); piece on order rows; logout-all. Small, and they touch screens that already exist.
2. **Phase 6:** settings changes (going public, selling off, deletion check, Hide from my Collection, seller terms). Self-contained.
3. **Phase 3 and 3A:** host ticket types, checkout with polling (stop on every end state), free tickets through `/orders`, then the wallet buttons.
4. **Phase 4:** the refund button, then the scanner (online first, then the offline manifest), then reschedule and cancel with the preview. The waitlist screens already work; check the deep links.
5. **Phase 5:** invites inbox, the artist's piece request screen, Lineup states, the bidding end picker, Exhibited.
6. **Phase 8:** listing fields and the Pickup address screen first (selling needs them), then the new checkout with live quotes, then tracking and the piece-page estimate. Installed apps can't check out on a Convelio server until this ships.
7. **Phase 7a, then 7b:** the first-bid card check and Ship to row, payment-failed and second-chance screens, then the post-win logic of 7b (`viewerOrder` first, then `autoCheckout.state`).
8. **Notifications:** map every `type` in the table above to its screen as each phase lands.
