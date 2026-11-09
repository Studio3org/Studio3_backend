# Frontend handoff

What changed on the backend, and what the app needs to do about it. Each backend phase adds a section here as it lands.

Read this against the app on `origin/main`. "No app change" means the current app code already works with the new backend.

Responses keep the usual envelope `{success, message, data}`. Errors are `{success: false, message}` with the HTTP status shown.

| Phase | Status | App work needed |
|---|---|---|
| 1. Security | Done on branch `phase2` | Small: error messages on sign-in and code screens |
| 2. Refunds and cancellation | Done on branch `phase2` | Order detail: cancel buttons, artist cancel confirmation, buyer "Report an issue" |
| 3. Ticketing core | Not started | — |
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
