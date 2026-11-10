"""A fake Convelio for the shipping tests (Phase 8 and 7b): answers like the v2 API, records
what it was sent, and can return an instant price, a custom quote, a timeout or a refusal."""
import hashlib
import hmac
import json
import uuid

import pytest

from src.modules.shipping import convelio_client

KEY = "sk_test_fake_convelio"


class FakeConvelio:
    def __init__(self):
        self.quotes = []
        self.orders = []
        self.mode = "instant"      # instant | processing
        self.order_mode = "ok"     # ok | timeout | refuse
        self.estimate_mode = "422"
        self.price = {"currency_code": "USD", "vat_excluded_amount": 340_00,
                      "vat_included_amount": 340_00, "vat_amount": 0, "insurance_amount": 25_00}

    def create_quote(self, body):
        self.quotes.append(body)
        qid = f"q_{uuid.uuid4().hex[:10]}"
        if self.mode == "processing":
            return {"id": qid, "quote_reference_number": "QVO-1", "status": "processing", "price": None}
        return {"id": qid, "quote_reference_number": "QVO-1", "status": "created", "price": self.price}

    def get_quote(self, quote_id):
        return {"id": quote_id, "quote_reference_number": "QVO-1", "status": "created", "price": self.price}

    def create_order(self, body):
        if self.order_mode == "timeout":
            raise convelio_client.ConvelioTimeout("slow", 504)
        if self.order_mode == "refuse":
            raise convelio_client.ConvelioError("Shipping request was refused: bad quote", 400)
        self.orders.append(body)
        return {"id": f"o_{len(self.orders)}", "order_reference_number": f"CVO-{len(self.orders)}",
                "tracking_link": "https://track.convelio.test/x", "dashboard_order_link": "https://dash.test/x",
                "price": self.price}

    def estimate(self, body):
        if self.estimate_mode == "422":
            raise convelio_client.ConvelioError("Shipping couldn't be priced: no estimate", 422)
        return {"price": {"vat_excluded_amount": 300_00, "vat_included_amount": 300_00}}


@pytest.fixture
def convelio(monkeypatch):
    fake = FakeConvelio()
    monkeypatch.setenv("CONVELIO_ENABLED", "true")
    monkeypatch.setenv("CONVELIO_API_KEY", KEY)
    for name in ("create_quote", "get_quote", "create_order", "estimate"):
        monkeypatch.setattr(convelio_client, name, getattr(fake, name))
    return fake


def signed_webhook(client, event: dict):
    raw = json.dumps(event).encode()
    sig = hmac.new(KEY.encode(), raw, hashlib.sha256).hexdigest()
    return client.post("/api/webhooks/convelio", data=raw, content_type="application/json",
                       headers={"X-Convelio-signature": sig})
