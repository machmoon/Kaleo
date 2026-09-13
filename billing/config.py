"""Configuration, validated once, and never echoed.

``slackbot/config.py``'s shape: validate at construction and name what is
missing, because a half-configured billing integration that starts and dies on
the first real payment is worse than one that refuses to start.

Nothing here ever returns a key. ``describe()`` exists so a health endpoint
can say "configured" without becoming the environment-variable dump the
Stripe security guidance explicitly warns against building.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .errors import ConfigError
from .transport import mask_key

__all__ = ["STRIPE_API_BASE", "STRIPE_API_VERSION", "BillingConfig"]

#: Pinned. An unpinned version lets Stripe reshape a response under a parser
#: with no way to notice -- the same argument ``meetings/config.py`` makes for
#: pinning the Meet API to v2.
STRIPE_API_VERSION = "2026-07-29.dahlia"
STRIPE_API_BASE = "https://api.stripe.com"


@dataclass(frozen=True)
class BillingConfig:
    api_key: str
    webhook_secret: str
    price_id: str
    credit_mkcu_per_purchase: int
    success_url: str
    cancel_url: str
    rate_cents_per_kcu: int = 225
    api_base: str = STRIPE_API_BASE
    api_version: str = STRIPE_API_VERSION

    def __post_init__(self) -> None:
        if not self.api_key:
            raise ConfigError("STRIPE_API_KEY is not set")
        if not self.api_key.startswith(("rk_", "sk_")):
            raise ConfigError(
                "STRIPE_API_KEY should start with 'rk_' (restricted) or 'sk_'"
            )
        if not self.webhook_secret.startswith("whsec_"):
            raise ConfigError("STRIPE_WEBHOOK_SECRET should start with 'whsec_'")
        if not self.price_id.startswith("price_"):
            raise ConfigError(
                "STRIPE_PRICE_ID should be a price id starting with 'price_'"
            )
        if self.credit_mkcu_per_purchase <= 0:
            raise ConfigError("STRIPE_CREDIT_MKCU must be a positive integer")
        # The allowlist is worthless if api_base itself can point elsewhere:
        # every request carries the key in an Authorization header.
        from .transport import ensure_stripe_url

        ensure_stripe_url(f"{self.api_base}/v1")
        for name, url in (("success", self.success_url), ("cancel", self.cancel_url)):
            if not url.startswith(("https://", "http://localhost", "kaleo://")):
                raise ConfigError(
                    f"{name}_url must be https, localhost, or a kaleo:// deep link"
                )

    def __repr__(self) -> str:
        """The dataclass default printed the key and the signing secret in full.

        ``describe()`` honoured "never echoed"; ``__repr__`` silently did not,
        and ``__repr__`` is what ends up in a traceback, a log line and a
        crash report.
        """
        return f"BillingConfig({self.describe()})"

    __str__ = __repr__

    @property
    def uses_restricted_key(self) -> bool:
        return self.api_key.startswith("rk_")

    @property
    def is_live(self) -> bool:
        return "_live_" in self.api_key

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> BillingConfig:
        src = os.environ if env is None else env
        missing = [
            k
            for k in ("STRIPE_API_KEY", "STRIPE_WEBHOOK_SECRET", "STRIPE_PRICE_ID")
            if not src.get(k, "").strip()
        ]
        if missing:
            raise ConfigError(f"missing required environment: {', '.join(missing)}")
        try:
            credit = int(src.get("STRIPE_CREDIT_MKCU", "8888"))
        except ValueError:
            raise ConfigError("STRIPE_CREDIT_MKCU must be an integer") from None
        try:
            rate = int(src.get("KALEO_RATE_CENTS_PER_KCU", "225"))
        except ValueError:
            raise ConfigError("KALEO_RATE_CENTS_PER_KCU must be an integer") from None
        return cls(
            api_key=src["STRIPE_API_KEY"].strip(),
            webhook_secret=src["STRIPE_WEBHOOK_SECRET"].strip(),
            price_id=src["STRIPE_PRICE_ID"].strip(),
            credit_mkcu_per_purchase=credit,
            success_url=src.get("STRIPE_SUCCESS_URL", "kaleo://billing/success").strip(),
            cancel_url=src.get("STRIPE_CANCEL_URL", "kaleo://billing/cancel").strip(),
            rate_cents_per_kcu=rate,
        )

    def describe(self) -> dict[str, object]:
        """Safe to log and safe to serve. Contains no secret."""
        return {
            "api_key": mask_key(self.api_key),
            "key_kind": "restricted" if self.uses_restricted_key else "secret",
            "mode": "live" if self.is_live else "test",
            "webhook_secret": f"<set, {len(self.webhook_secret)} chars>",
            "price_id": self.price_id,
            "credit_mkcu_per_purchase": self.credit_mkcu_per_purchase,
            "rate_cents_per_kcu": self.rate_cents_per_kcu,
            "api_version": self.api_version,
        }
