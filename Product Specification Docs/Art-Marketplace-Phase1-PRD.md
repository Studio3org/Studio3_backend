# Product Requirements Document
## Art Marketplace — Phase 1: Payment, Escrow-Style Release & Manual Delivery

**Document version:** 1.0
**Status:** Draft for review
**Owner:** [Product/Engineering Lead]
**Last updated:** August 19, 2026

---

## 1. Overview

### 1.1 Purpose

This document defines the requirements for **Phase 1** of the art marketplace platform, covering the end-to-end flow from artwork purchase to artist payout. The core principle of this phase is that **artists are paid only after the collector confirms receipt of the artwork**, with shipping/logistics handled manually by the platform team.

### 1.2 Problem statement

Art is a high-value, low-frequency purchase category where trust between buyer and seller is low by default — collectors are paying large sums for physical goods sight-unseen (relative to in-person inspection), and artists risk non-payment or chargebacks. The platform must:

- Collect payment securely upfront
- Prevent artists from receiving funds before the collector has verified the artwork arrived in good condition
- Provide a manual but trackable logistics process (since automated shipping integration is out of scope for Phase 1)
- Handle disputes (damage, wrong item, non-delivery) without automated tooling

### 1.3 Goals

| Goal | Description |
|---|---|
| G1 | Enable collectors to pay for artwork securely via Stripe |
| G2 | Ensure artist payout is withheld until delivery is confirmed |
| G3 | Allow platform team to manually manage shipping and tracking |
| G4 | Provide a clear, auditable order and payout state machine |
| G5 | Support manual dispute resolution (refund or release) |
| G6 | Correctly account for platform commission, Stripe fees, and shipping charges |

### 1.4 Non-goals (explicitly out of scope for Phase 1)

- Easyship or any third-party shipping API integration
- Automatic tracking synchronization or delivery detection
- Automated courier booking
- Automated dispute resolution or arbitration workflows
- Multi-currency support (assumed single currency, e.g., INR, unless stated otherwise)
- Installment/partial payments
- Auctions or bidding flows

---

## 2. Key Stakeholders / User Roles

| Role | Description |
|---|---|
| **Collector** | Buyer who purchases artwork |
| **Artist** | Seller who lists artwork and receives payout after confirmed delivery |
| **Platform Admin/Ops Team** | Internal team that manually arranges courier pickup, enters tracking, and resolves disputes |
| **Platform (System)** | Backend, database, and Stripe integration layer |

---

## 3. Core User Flow

```
Collector buys artwork
        ↓
Collector pays (Stripe Payments)
        ↓
Payment goes to platform's Stripe balance
        ↓
Order = PAID
        ↓
Platform manually arranges courier pickup (FedEx/UPS)
        ↓
Artist hands artwork to courier — Order = SHIPPED
        ↓
Platform manually enters tracking info
        ↓
Tracking shared with Collector & Artist
        ↓
Courier delivers artwork
        ↓
Order = AWAITING_CONFIRMATION
        ↓
Collector confirms receipt (or reports an issue)
        ↓
Order = COMPLETED  →  Artist payout released via Stripe Connect
        (or)
Order = DISPUTED  →  Manual resolution → Refund or Release
```

---

## 4. Functional Requirements

### 4.1 Artist Onboarding (Stripe Connect)

| ID | Requirement |
|---|---|
| FR-1.1 | Artists must complete Stripe Connect onboarding before listing artwork for sale |
| FR-1.2 | Platform stores `stripe_account_id` and `payouts_enabled` flag per artist |
| FR-1.3 | Platform must check `payouts_enabled` before allowing an artwork to go live for purchase |
| FR-1.4 | Platform provides an endpoint/UI for artists to check their Connect onboarding status |

**API:**
```
POST /artists/connect            → initiate onboarding, returns Stripe onboarding link
GET  /artists/connect/status     → returns payouts_enabled and account status
```

### 4.2 Collector Payment

| ID | Requirement |
|---|---|
| FR-2.1 | Collector initiates checkout; backend creates a Stripe PaymentIntent for the full order amount (artwork price + shipping estimate, if bundled) |
| FR-2.2 | Payment success/failure must be determined **only** via Stripe webhook events, never by frontend confirmation alone |
| FR-2.3 | On `payment_intent.succeeded`, order status transitions to `PAID` |
| FR-2.4 | On `payment_intent.payment_failed`, order status transitions to `FAILED` |
| FR-2.5 | Platform must log all Stripe webhook events for audit purposes |
| FR-2.6 | Full payment amount is held in the platform's Stripe balance — no automatic split occurs at this stage |

**API:**
```
POST /payments/create-payment-intent
POST /payments/webhook
```

**Relevant Stripe webhook events:**
```
payment_intent.succeeded
payment_intent.payment_failed
charge.refunded
transfer.created
transfer.failed
```

### 4.3 Order Management

| ID | Requirement |
|---|---|
| FR-3.1 | Every purchase creates an `Order` record with a defined state machine (see Section 6) |
| FR-3.2 | Orders store collector info, artist info, artwork info, and computed price breakdown (commission, shipping, Stripe fee estimate) |
| FR-3.3 | Collector and artist can each view their relevant order details and status |
| FR-3.4 | Admin/ops team can view all orders and manually transition shipment-related states |

**API:**
```
POST /orders
GET  /orders
GET  /orders/:id
POST /orders/:id/confirm-received
```

### 4.4 Manual Shipping & Tracking

| ID | Requirement |
|---|---|
| FR-4.1 | Once an order is `PAID`, platform ops team manually contacts a courier (FedEx, UPS, or other) using stored pickup (artist) and delivery (collector) addresses |
| FR-4.2 | Ops team manually updates order status to `SHIPPED` once courier confirms pickup |
| FR-4.3 | Ops team manually enters courier name and tracking number into the system |
| FR-4.4 | Tracking info is visible to Collector, Artist, and Platform admin |
| FR-4.5 | Order status transitions to `AWAITING_CONFIRMATION` once ops team marks the order as delivered (manually, since no delivery webhook exists) |

**API:**
```
POST  /orders/:id/shipment
PATCH /orders/:id/shipment
```

**Shipment data captured:**
```
courier
tracking_number
shipment_date
status
```

### 4.5 Delivery Confirmation

| ID | Requirement |
|---|---|
| FR-5.1 | Collector must be able to mark an order as "Received" once artwork arrives |
| FR-5.2 | On confirmation, backend records `received = true` and `received_at` timestamp |
| FR-5.3 | Order status transitions to `COMPLETED` |
| FR-5.4 | Collector must also be able to report an issue instead of confirming (damaged, wrong item, not received) |
| FR-5.5 | Reporting an issue transitions the order to `DISPUTED` and blocks payout release |

### 4.6 Artist Payout (Stripe Connect Transfer)

| ID | Requirement |
|---|---|
| FR-6.1 | Payout release is triggered **only** by backend logic after order reaches `COMPLETED` — never directly from a frontend request |
| FR-6.2 | Payout amount = artwork price − platform commission (shipping and Stripe fees are accounted for separately per the fee model in Section 7) |
| FR-6.3 | Transfer operation must be **idempotent** — repeated calls/clicks must not create duplicate transfers |
| FR-6.4 | Payout has its own state machine, independent of order status (see Section 6.2) |
| FR-6.5 | On `transfer.failed` webhook, payout status transitions to `TRANSFER_FAILED` and triggers an admin alert |

**API:**
```
POST /orders/:id/release-payment
GET  /transfers/:id
```

### 4.7 Dispute Handling (Manual, Phase 1)

| ID | Requirement |
|---|---|
| FR-7.1 | Disputed orders must be visible in an admin queue |
| FR-7.2 | Admin can manually resolve a dispute with one of two outcomes: **Refund Collector** or **Release Artist Payment** |
| FR-7.3 | Refund action uses Stripe's refund API against the original PaymentIntent/Charge |
| FR-7.4 | Release action proceeds through the standard payout flow (FR-6) |
| FR-7.5 | All dispute resolutions must be logged with admin identity, timestamp, and resolution reason |

---

## 5. Non-Functional Requirements

| ID | Requirement |
|---|---|
| NFR-1 | All payment-affecting decisions (success, failure, payout release) must be driven by backend/webhook logic, never trusted from client input |
| NFR-2 | Transfer/payout operations must be idempotent (see FR-6.3) |
| NFR-3 | All Stripe webhook events must be verified via Stripe signature validation |
| NFR-4 | All state transitions (order and payout) must be logged with timestamps for auditability |
| NFR-5 | Sensitive data (bank details, KYC) is handled entirely by Stripe — platform never stores raw bank/KYC data |
| NFR-6 | System must support at least [X] concurrent orders without race conditions on payout logic (define X with engineering) |
| NFR-7 | Admin actions (dispute resolution, manual shipment updates) must be access-controlled and audit-logged |

---

## 6. State Machines

### 6.1 Order Status

```
PENDING_PAYMENT → PAID → SHIPPED → AWAITING_CONFIRMATION → COMPLETED

Alternate paths:
PENDING_PAYMENT → FAILED
PAID → REFUNDED
SHIPPED → DISPUTED
AWAITING_CONFIRMATION → DISPUTED
```

### 6.2 Artist Payout Status

```
PENDING → READY_TO_RELEASE → RELEASED

Alternate path:
PENDING / READY_TO_RELEASE → TRANSFER_FAILED
```

---

## 7. Financial Model

### 7.1 Money flow summary

```
Collector pays full amount (artwork + shipping, if bundled)
        ↓
Stripe deducts processing fee immediately
        ↓
Remaining balance sits in Platform's Stripe account
        ↓
Platform commission stays in platform balance (not transferred)
        ↓
Artist share transferred via Stripe Connect ONLY after order = COMPLETED
```

### 7.2 Worked example

```
Artwork price                          ₹100,000
Shipping (added at checkout)             ₹2,000
────────────────────────────────────────────────
Collector pays                         ₹102,000

Stripe processing fee (~2% + fixed)      -₹2,040
────────────────────────────────────────────────
Net in platform Stripe balance          ₹99,960

Platform commission (10% of artwork)    ₹10,000
Shipping cost paid to courier            ₹2,000  (paid by platform, outside Stripe Connect)
────────────────────────────────────────────────
Platform retains (net of Stripe fee)   ~₹9,960 – ₹12,000 depending on fee absorption policy
Artist payout (via Stripe Connect)      ₹90,000  → released only on COMPLETED
```

### 7.3 Key financial decisions requiring stakeholder sign-off

| Decision | Options |
|---|---|
| Who absorbs the Stripe processing fee? | (a) Platform absorbs from commission, or (b) passed to collector as a line item |
| Is shipping bundled into the PaymentIntent or collected separately? | (a) Added to checkout total, or (b) invoiced/settled outside Stripe |
| How is the courier paid? | Outside Stripe Connect — via platform's own operating funds/bank account |
| Platform commission rate | To be finalized (10% used as example throughout this document) |

### 7.4 Regulatory flag (India-specific — requires verification)

⚠️ **Action item before finalizing architecture:** Stripe's support for domestic India-to-India marketplace payment flows via Stripe Connect has historical regulatory restrictions (RBI rules on payment aggregators). This must be confirmed directly with Stripe or payments counsel before implementation. If Stripe Connect is not viable for domestic INR transactions, an alternative marketplace payment provider (e.g., Razorpay Route, Cashfree) may be required for the artist payout leg specifically.

---

## 8. Data Model

### 8.1 Core tables

```
users
artists
artworks
orders
order_items
payments
transfers
shipments
```

### 8.2 Relationships

```
users
  └── artists
        └── artworks
              └── orders
                    ├── order_items
                    ├── payments
                    ├── shipments
                    └── transfers
```

### 8.3 Key fields (illustrative, not exhaustive)

**artists**
```
id
user_id
stripe_account_id
payouts_enabled
```

**orders**
```
id
collector_id
artist_id
artwork_id
status                 (PENDING_PAYMENT | PAID | SHIPPED | AWAITING_CONFIRMATION | COMPLETED | FAILED | REFUNDED | DISPUTED)
artwork_price
shipping_charge
platform_commission
received
received_at
```

**shipments**
```
id
order_id
courier
tracking_number
shipment_date
status
```

**transfers**
```
id
order_id
artist_id
amount
status                 (PENDING | READY_TO_RELEASE | RELEASED | TRANSFER_FAILED)
stripe_transfer_id
```

---

## 9. API Summary

| Area | Endpoint | Method |
|---|---|---|
| Payments | `/payments/create-payment-intent` | POST |
| Payments | `/payments/webhook` | POST |
| Orders | `/orders` | POST |
| Orders | `/orders` | GET |
| Orders | `/orders/:id` | GET |
| Orders | `/orders/:id/confirm-received` | POST |
| Shipping | `/orders/:id/shipment` | POST |
| Shipping | `/orders/:id/shipment` | PATCH |
| Artist Connect | `/artists/connect` | POST |
| Artist Connect | `/artists/connect/status` | GET |
| Transfers | `/orders/:id/release-payment` | POST |
| Transfers | `/transfers/:id` | GET |

---

## 10. System Architecture (High-Level)

```
                 COLLECTOR
                     │
                     ▼
                FRONTEND
                     │
                     ▼
              NODE.JS BACKEND
                     │
          ┌──────────┴──────────┐
          ▼                     ▼
  PostgreSQL + Prisma          STRIPE
                          ┌──────┴───────┐
                          ▼              ▼
                     Payments        Connect
                    (collector      (artist
                      pays)          payout)

Order created → Manual shipping (Platform Ops → FedEx/UPS → Artist)
             → Tracking shared (Collector, Artist, Platform)
             → Delivery → Collector confirms receipt
             → Backend releases payout via Stripe Connect → Artist bank account
```

---

## 11. Phase 1 Scope Summary

| Feature | In Scope |
|---|---|
| Stripe Payments (collector → platform) | ✅ |
| Stripe Connect (platform → artist) | ✅ |
| Artist Stripe onboarding | ✅ |
| Manual FedEx/UPS booking | ✅ |
| Manual tracking entry & display | ✅ |
| Collector delivery confirmation | ✅ |
| Manual dispute resolution | ✅ |
| Idempotent payout transfers | ✅ |
| Easyship integration | ❌ |
| Automatic tracking sync | ❌ |
| Automatic delivery detection | ❌ |
| Automated dispute system | ❌ |
| Automated courier booking | ❌ |

---

## 12. Open Questions / Risks

| # | Question / Risk | Owner |
|---|---|---|
| 1 | Confirm Stripe Connect viability for India-domestic transactions (RBI/aggregator rules) | Payments/Legal |
| 2 | Finalize whether Stripe processing fee is absorbed by platform or passed to collector | Product |
| 3 | Finalize whether shipping is bundled into checkout or settled separately | Product |
| 4 | Define SLA for manual ops team actions (shipment booking, tracking entry) to avoid collector complaints | Ops |
| 5 | Define timeout/escalation policy if collector never confirms receipt or reports an issue | Product |
| 6 | Define refund policy specifics (partial refunds? platform commission refunded too?) | Product/Legal |
| 7 | Confirm data retention/compliance requirements for KYC data (handled by Stripe, but confirm no local storage) | Engineering/Legal |

---

## 13. Success Metrics (Phase 1)

| Metric | Target (example — finalize with stakeholders) |
|---|---|
| % of orders reaching COMPLETED without dispute | > 90% |
| Average time from PAID to SHIPPED (manual ops SLA) | < 48 hours |
| Payout transfer failure rate | < 1% |
| Dispute resolution time (manual) | < 5 business days |
| Duplicate/double-payout incidents | 0 |

---

*End of Phase 1 PRD.*
