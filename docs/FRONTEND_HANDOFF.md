# Frontend handoff

What changed on the backend, and what the app needs to do about it. Each backend phase adds a section here as it lands.

Read this against the app on `origin/main`. "No app change" means the current app code already works with the new backend.

Responses keep the usual envelope `{success, message, data}`. Errors are `{success: false, message}` with the HTTP status shown.

| Phase | Status | App work needed |
|---|---|---|
| 1. Security | Done on branch `phase2` | Small: error messages on sign-in and code screens |
| 2. Refunds and cancellation | Done on branch `phase2` | Order detail: cancel buttons, artist cancel confirmation, buyer "Report an issue" |
| 2A. Quick fixes | Done on branch `phase2` | Use the piece on order rows; logout-all message; founding-rate end date |
| 3. Ticketing core | Done (not yet committed) | Host ticket-type screens; free tickets through `/orders`; checkout polling; tickets screen already works |
| 4. Ticket refunds, waitlist, check-in | Not started | — |
| 5. Event invites and approvals | Not started | — |
| 6. Auction winner deadline | Not started | — |
| 7. Convelio shipping | Not started | — |

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

`waitlist` is still not sent. It arrives in Phase 4, and B22 says to leave it out until then.

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
| Viewer holds paid tickets | — | Free tickets are voided, then `409` "Your paid tickets are still valid…". Refunds come in Phase 4 |

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
