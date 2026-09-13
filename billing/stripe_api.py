"""The two Stripe calls this package makes, built by hand over the seam.

Checkout Sessions only. Per Stripe's own integration routing, one-time
payments belong in Checkout Sessions -- not the Charges API (never), and not a
raw PaymentIntent, which is for off-session work.

Deliberately absent: ``payment_method_types``. Omitting it enables dynamic
payment methods, which Stripe selects per customer, currency and device.
Hardcoding ``['card']`` is a conversion bug, and Stripe's guidance calls it out
as the trap. Payment methods are configured in the Dashboard instead.
"""

from __future__ import annotations

import re
import secrets
import string
from typing import Any

from .accounts import AccountId
from .config import BillingConfig
from .errors import StripeError
from .transport import HttpRequest, Transport, form_encode, urllib_transport

__all__ = [
    "charge_overage",
    "create_checkout_session",
    "create_setup_session",
    "integration_label",
    "retrieve_session",
]

_LETTERS = string.ascii_lowercase


def integration_label(
    prefix: str = "kaleo-credits", *, suffix: str | None = None
) -> str:
    """``integration_identifier`` for the Dashboard, with the 8-letter suffix
    Stripe's guidance asks for on API 2026-03-25.dahlia and later."""
    tail = suffix or "".join(secrets.choice(_LETTERS) for _ in range(8))
    return f"{prefix}-{tail}"


def _headers(config: BillingConfig, *, idempotency_key: str | None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Stripe-Version": config.api_version,
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "kaleo-billing/1 (stdlib urllib)",
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _unwrap(response: Any) -> dict[str, Any]:
    body = response.json()
    if response.status >= 400:
        err = body.get("error", {}) if isinstance(body, dict) else {}
        raise StripeError(
            err.get("message", "Stripe returned an error"),
            status=response.status,
            code=err.get("code"),
        )
    if not isinstance(body, dict):
        raise StripeError("Stripe returned a non-object body")
    return body


def create_checkout_session(
    config: BillingConfig,
    account: AccountId,
    *,
    quantity: int = 1,
    transport: Transport = urllib_transport,
    idempotency_key: str | None = None,
    integration_identifier: str | None = None,
    customer_email: str | None = None,
) -> dict[str, Any]:
    """Create a Checkout Session for one credit pack.

    The account travels in **both** ``client_reference_id`` and ``metadata``,
    and the credit amount travels in metadata too -- so fulfillment reads what
    to grant from the event Stripe signed, never from anything a client sent.
    """
    if quantity < 1:
        raise StripeError("quantity must be at least 1")
    params: dict[str, Any] = {
        "mode": "payment",
        "line_items": [{"price": config.price_id, "quantity": quantity}],
        "success_url": config.success_url,
        "cancel_url": config.cancel_url,
        "client_reference_id": str(account),
        "metadata": {
            "kaleo_account": str(account),
            "kaleo_credit_mkcu": str(config.credit_mkcu_per_purchase * quantity),
        },
        "payment_intent_data": {
            "metadata": {"kaleo_account": str(account)},
        },
        "integration_identifier": integration_identifier or integration_label(),
        "customer_email": customer_email,
    }
    request = HttpRequest(
        url=f"{config.api_base}/v1/checkout/sessions",
        method="POST",
        headers=_headers(config, idempotency_key=idempotency_key),
        body=form_encode(params),
    )
    return _unwrap(transport(request))


def retrieve_session(
    config: BillingConfig, session_id: str, *, transport: Transport = urllib_transport
) -> dict[str, Any]:
    """Read a session back. Used for reconciliation, never for fulfillment."""
    # Whole-string match, not a prefix: a prefix check lets the rest of the
    # string become extra path and query on an authenticated Stripe request
    # ("cs_a/../../v1/customers?limit=100"), which the host allowlist cannot
    # catch because the host really is Stripe.
    if not re.fullmatch(r"cs_[A-Za-z0-9_]{1,255}", session_id):
        raise StripeError("not a well-formed Checkout Session id")
    request = HttpRequest(
        url=f"{config.api_base}/v1/checkout/sessions/{session_id}",
        method="GET",
        headers=_headers(config, idempotency_key=None),
    )
    return _unwrap(transport(request))


def create_setup_session(
    config: BillingConfig,
    account: AccountId,
    *,
    customer: str | None = None,
    transport: Transport = urllib_transport,
    idempotency_key: str | None = None,
    currency: str = "usd",
) -> dict[str, Any]:
    """Checkout in ``setup`` mode: save a payment method, charge nothing now.

    This is the prerequisite for overage. Billing usage after the fact means
    charging **off-session**, and an off-session charge needs a payment method
    already on file with the customer's consent to reuse it. Stripe's guidance
    is explicit that SetupIntents -- not the deprecated Sources or Tokens
    APIs -- are how a payment method is saved for later.
    """
    params: dict[str, Any] = {
        "mode": "setup",
        # Required in setup mode when payment_method_types is unset (which it
        # always is here -- omitting it is what enables dynamic payment
        # methods). Without it the call 400s and no card can ever be saved,
        # so the overage credit line becomes uncollectable.
        "currency": currency,
        # So the session yields a cus_ id to charge against later.
        "customer_creation": "always" if customer is None else None,
        "success_url": config.success_url,
        "cancel_url": config.cancel_url,
        "client_reference_id": str(account),
        "customer": customer,
        "metadata": {"kaleo_account": str(account)},
        "integration_identifier": integration_label("kaleo-overage-setup"),
    }
    request = HttpRequest(
        url=f"{config.api_base}/v1/checkout/sessions",
        method="POST",
        headers=_headers(config, idempotency_key=idempotency_key),
        body=form_encode(params),
    )
    return _unwrap(transport(request))


def charge_overage(
    config: BillingConfig,
    account: AccountId,
    *,
    amount_cents: int,
    customer: str,
    payment_method: str,
    transport: Transport = urllib_transport,
    idempotency_key: str | None = None,
    currency: str = "usd",
) -> dict[str, Any]:
    """Charge delivered-but-unpaid compute, off-session.

    ``idempotency_key`` is not optional in practice and the caller should key
    it on the settlement window, not on the clock: a retried settlement
    without one bills the same overage twice, and the customer is not present
    to notice.

    An off-session charge can come back needing the customer -- a bank asking
    for authentication. That surfaces as ``StripeError`` with code
    ``authentication_required``; the answer is to ask them to complete it, not
    to retry, and never to cut off a run that already happened.
    """
    if amount_cents <= 0:
        raise StripeError("an overage charge must be positive")
    params: dict[str, Any] = {
        "amount": amount_cents,
        "currency": currency,
        "customer": customer,
        "payment_method": payment_method,
        "off_session": True,
        "confirm": True,
        "description": "Hardy compute overage",
        "metadata": {"kaleo_account": str(account), "kaleo_kind": "overage"},
    }
    request = HttpRequest(
        url=f"{config.api_base}/v1/payment_intents",
        method="POST",
        headers=_headers(config, idempotency_key=idempotency_key),
        body=form_encode(params),
    )
    return _unwrap(transport(request))
