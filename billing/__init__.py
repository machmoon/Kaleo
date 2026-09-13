"""Usage-credit billing for Kaleo, in the shape ``googleapps/`` established.

Stdlib only -- no ``stripe`` SDK -- because this makes two REST calls and the
repo already talks to Slack, Google and Meet the same way. Every outbound
request crosses ``transport.Transport`` so the suite runs offline with no key.

The model is Devin's, scaled: prepaid compute units that draw down, a small
entry payment that becomes credit, and auto-reload at a threshold -- with a
monthly cap, because this bills an agent and a retry loop is the normal
failure mode.
"""

from __future__ import annotations

from .accounts import (
    AccountId,
    AccountResolver,
    SingleAccountResolver,
    StripeCustomerResolver,
)
from .config import STRIPE_API_VERSION, BillingConfig
from .errors import (
    BillingError,
    ConfigError,
    LedgerError,
    ReplayedEvent,
    SpendCapExceeded,
    StripeError,
    WebhookSignatureError,
)
from .fulfillment import Outcome, handle_event
from .ledger import (
    Balance,
    Entry,
    MemoryLedger,
    OveragePolicy,
    Reservation,
    TopUpPolicy,
)
from .stripe_api import (
    charge_overage,
    create_checkout_session,
    create_setup_session,
    retrieve_session,
)
from .units import Meter, cents_to_display, kcu, mkcu_to_display, price_mkcu
from .webhook import verify_signature

__all__ = [
    "STRIPE_API_VERSION",
    "AccountId",
    "AccountResolver",
    "Balance",
    "BillingConfig",
    "BillingError",
    "ConfigError",
    "Entry",
    "LedgerError",
    "MemoryLedger",
    "Meter",
    "OveragePolicy",
    "Outcome",
    "ReplayedEvent",
    "Reservation",
    "SingleAccountResolver",
    "SpendCapExceeded",
    "StripeCustomerResolver",
    "StripeError",
    "TopUpPolicy",
    "WebhookSignatureError",
    "cents_to_display",
    "charge_overage",
    "create_checkout_session",
    "create_setup_session",
    "handle_event",
    "kcu",
    "mkcu_to_display",
    "price_mkcu",
    "retrieve_session",
    "verify_signature",
]
