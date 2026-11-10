"""A thin client for Convelio's Public API v2 (developers.convelio.com).

Every call sends `?currency=USD` (the API defaults to EUR), the v2 content type and
`Authorization: token <key>`. Errors come back as problem+json; a 422 carries per-field
messages, which become an AppError the app can show. Timeouts are generous: a quote takes
2–9 seconds in the sandbox.

There's no idempotency key, no read-order and no cancel-order call in the API, which is why
booking_service never retries a booking whose outcome is unknown.
"""
from __future__ import annotations

from typing import Optional

import httpx

from src.modules.shipping import shipping_config
from src.shared.utils.app_error import AppError
from src.shared.utils.logger import get_logger

logger = get_logger(__name__)

TIMEOUT = httpx.Timeout(30.0, connect=10.0)
CONTENT_TYPE = "application/vnd.convelio-shipping.v2+json"


class ConvelioError(AppError):
    """Convelio refused or failed a request. `status_code` is ours (400/422/502), `upstream`
    is theirs."""

    def __init__(self, message: str, status_code: int = 502, *, upstream: Optional[int] = None,
                 fields: Optional[dict] = None):
        super().__init__(message, status_code)
        self.upstream = upstream
        self.fields = fields or {}


class ConvelioTimeout(ConvelioError):
    """No answer: the request may or may not have been carried out."""


def _client() -> httpx.Client:
    key = shipping_config.api_key()
    if not key:
        raise ConvelioError("Shipping isn't set up yet.", 503)
    return httpx.Client(
        base_url=shipping_config.base_url(),
        timeout=TIMEOUT,
        headers={"Authorization": f"token {key}", "Content-Type": CONTENT_TYPE,
                 "Accept": "application/json"},
    )


def _problem(response: httpx.Response) -> ConvelioError:
    try:
        body = response.json()
    except ValueError:
        body = {}
    fields = {}
    for entry in body.get("validation_messages") or body.get("violations") or []:
        if isinstance(entry, dict):
            fields[entry.get("propertyPath") or entry.get("field") or "?"] = (
                entry.get("message") or entry.get("title") or "")
    if isinstance(body.get("validation_messages"), dict):
        fields.update({k: str(v) for k, v in body["validation_messages"].items()})
    detail = body.get("detail") or body.get("title") or response.text[:300]
    logger.warning("Convelio %s on %s: %s %s", response.status_code, response.request.url.path,
                   detail, fields)
    if response.status_code == 422:
        return ConvelioError(f"Shipping couldn't be priced: {detail}", 422,
                             upstream=422, fields=fields)
    if response.status_code in (400, 404):
        return ConvelioError(f"Shipping request was refused: {detail}", 400,
                             upstream=response.status_code, fields=fields)
    return ConvelioError("The shipping service isn't answering right now. Please try again.",
                         502, upstream=response.status_code)


def _call(method: str, path: str, body: Optional[dict] = None) -> dict:
    try:
        with _client() as client:
            response = client.request(method, path, params={"currency": "USD"}, json=body)
    except httpx.TimeoutException as exc:
        raise ConvelioTimeout("The shipping service took too long to answer.", 504) from exc
    except httpx.HTTPError as exc:
        raise ConvelioError("The shipping service isn't reachable right now.", 502) from exc
    if response.status_code >= 400:
        raise _problem(response)
    return response.json() if response.content else {}


def estimate(body: dict) -> dict:
    return _call("POST", "/shipping/estimate/price", body)


def create_quote(body: dict) -> dict:
    return _call("POST", "/shipping/quote", body)


def get_quote(quote_id: str) -> dict:
    return _call("GET", f"/shipping/quote/{quote_id}")


def create_order(body: dict) -> dict:
    return _call("POST", "/shipping/order", body)


def list_webhooks() -> list:
    data = _call("GET", "/webhook")
    return data if isinstance(data, list) else data.get("items") or data.get("data") or []


def create_webhook(url: str, event_name: str) -> dict:
    return _call("POST", "/webhook", {"url": url, "triggering_event_name": event_name})
