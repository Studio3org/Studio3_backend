"""A fake Stripe, plus real webhook signing.

Signature verification is deliberately NOT faked — `StripeStub.Webhook` delegates to the
real `stripe.Webhook`, and `stripe_signature` below computes a genuine HMAC. NFR-3 says
unsigned payloads must be rejected, and a stub that rubber-stamped them would leave the one
security property in this file untested.
"""
import hashlib
import hmac
import json
import time
import uuid
from typing import Any, Optional

import stripe as real_stripe


def stripe_signature(payload: bytes, secret: str, timestamp: Optional[int] = None) -> str:
    """Build a `Stripe-Signature` header the real library will accept."""
    ts = timestamp if timestamp is not None else int(time.time())
    signed_payload = f"{ts}.".encode() + payload
    digest = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


def make_event(event_type: str, obj: dict, *, event_id: Optional[str] = None) -> dict:
    """A Stripe event envelope. `obj` is whatever `data.object` should contain."""
    return {
        "id": event_id or f"evt_{uuid.uuid4().hex[:24]}",
        "object": "event",
        "api_version": "2024-06-20",
        "created": int(time.time()),
        "type": event_type,
        "data": {"object": obj},
    }


def post_webhook(
    client,
    event: dict,
    *,
    secret: str = "whsec_test_primary",
    signature: Optional[str] = None,
    payload: Optional[bytes] = None,
):
    """POST an event to /api/payments/webhook with a valid signature by default.

    `signature` and `payload` are overridable so the negative cases (tampered body, wrong
    secret, missing header) can be expressed without rebuilding the request by hand.
    """
    body = payload if payload is not None else json.dumps(event).encode()
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers["Stripe-Signature"] = signature
    elif signature is None and secret is not None:
        headers["Stripe-Signature"] = stripe_signature(body, secret)
    return client.post("/api/payments/webhook", data=body, headers=headers)


class _Namespace:
    """Maps attribute access onto the stub's bound methods (stripe.Transfer.create → ...)."""

    def __init__(self, **methods):
        self.__dict__.update(methods)


class StripeStub:
    """An in-memory Stripe.

    Records what was asked of it so tests can assert on call counts — "exactly one transfer
    was created" is the assertion most of the payout tests turn on.
    """

    def __init__(self) -> None:
        self.charges: dict[str, dict] = {}
        self.balance_transactions: dict[str, dict] = {}
        self.transfers: list[dict] = []
        self.refunds: list[dict] = []
        self.payment_intents: dict[str, dict] = {}
        self.accounts: dict[str, dict] = {}
        # Set to an exception to make the next Transfer.create raise, for the failure paths.
        self.transfer_error: Optional[Exception] = None

        self.Webhook = real_stripe.Webhook  # real verification, on purpose
        # construct_event catches stripe.error.SignatureVerificationError by name, so the
        # stub has to expose the genuine exception module, not a lookalike.
        self.error = real_stripe.error
        self.Transfer = _Namespace(create=self._transfer_create, list=self._transfer_list)
        self.Refund = _Namespace(create=self._refund_create)
        self.Charge = _Namespace(retrieve=self._charge_retrieve)
        self.BalanceTransaction = _Namespace(retrieve=self._balance_transaction_retrieve)
        self.PaymentIntent = _Namespace(
            create=self._payment_intent_create, retrieve=self._payment_intent_retrieve
        )
        self.Account = _Namespace(
            create=self._account_create,
            retrieve=self._account_retrieve,
            create_login_link=self._account_login_link,
        )
        self.AccountLink = _Namespace(create=self._account_link_create)

    # -- seeding -------------------------------------------------------------------------

    def add_charge(self, charge_id: str, *, fee_cents: int = 0, amount_cents: int = 0,
                   expand_balance_transaction: bool = True) -> dict:
        """Register a charge and its balance transaction.

        `expand_balance_transaction=False` stores the balance transaction as a bare id
        string instead of a nested object, which is the other shape Stripe returns and the
        one that forces `_stripe_fee_for_charge` down its retrieve branch.
        """
        bt_id = f"txn_{uuid.uuid4().hex[:24]}"
        bt = {"id": bt_id, "fee": fee_cents, "amount": amount_cents}
        self.balance_transactions[bt_id] = bt
        charge = {
            "id": charge_id,
            "amount": amount_cents,
            "balance_transaction": bt if expand_balance_transaction else bt_id,
        }
        self.charges[charge_id] = charge
        return charge

    def transfers_for(self, order_id) -> list[dict]:
        return [t for t in self.transfers if t.get("transfer_group") == f"order_{order_id}"]

    # -- API surface ---------------------------------------------------------------------

    def _transfer_create(self, **kwargs) -> dict:
        if self.transfer_error is not None:
            error, self.transfer_error = self.transfer_error, None
            raise error
        transfer = {
            "id": f"tr_{uuid.uuid4().hex[:24]}",
            "amount": kwargs.get("amount", 0),
            "currency": kwargs.get("currency", "usd"),
            "destination": kwargs.get("destination"),
            "transfer_group": kwargs.get("transfer_group"),
            "source_transaction": kwargs.get("source_transaction"),
            "metadata": kwargs.get("metadata", {}),
        }
        self.transfers.append(transfer)
        return transfer

    def _transfer_list(self, **kwargs) -> dict[str, Any]:
        group = kwargs.get("transfer_group")
        found = [t for t in self.transfers if group is None or t.get("transfer_group") == group]
        return {"object": "list", "data": found}

    def _refund_create(self, **kwargs) -> dict:
        refund = {
            "id": f"re_{uuid.uuid4().hex[:24]}",
            "charge": kwargs.get("charge"),
            "amount": kwargs.get("amount"),
            "reason": kwargs.get("reason"),
            "status": "succeeded",
        }
        self.refunds.append(refund)
        return refund

    def _charge_retrieve(self, charge_id, **kwargs) -> dict:
        if charge_id not in self.charges:
            raise real_stripe.error.InvalidRequestError(  # type: ignore[attr-defined]
                f"No such charge: {charge_id}", param="id"
            )
        return self.charges[charge_id]

    def _balance_transaction_retrieve(self, bt_id, **kwargs) -> dict:
        return self.balance_transactions[bt_id]

    def _payment_intent_create(self, **kwargs) -> dict:
        intent = {
            "id": f"pi_{uuid.uuid4().hex[:24]}",
            "client_secret": f"pi_secret_{uuid.uuid4().hex[:16]}",
            "amount": kwargs.get("amount"),
            "currency": kwargs.get("currency", "usd"),
            "status": "requires_payment_method",
            "transfer_group": kwargs.get("transfer_group"),
            "metadata": kwargs.get("metadata", {}),
        }
        self.payment_intents[intent["id"]] = intent
        return intent

    def _payment_intent_retrieve(self, intent_id, **kwargs) -> dict:
        return self.payment_intents[intent_id]

    def _account_create(self, **kwargs) -> dict:
        account = {
            "id": f"acct_{uuid.uuid4().hex[:16]}",
            "charges_enabled": False,
            "payouts_enabled": False,
            "details_submitted": False,
        }
        self.accounts[account["id"]] = account
        return account

    def _account_retrieve(self, account_id, **kwargs) -> dict:
        return self.accounts.get(
            account_id,
            {"id": account_id, "charges_enabled": True, "payouts_enabled": True,
             "details_submitted": True},
        )

    def _account_login_link(self, account_id, **kwargs) -> dict:
        return {"url": f"https://connect.stripe.test/login/{account_id}"}

    def _account_link_create(self, **kwargs) -> dict:
        return {"url": "https://connect.stripe.test/onboarding"}
