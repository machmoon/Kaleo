"""Setup status: what is configured, what is missing, and the exact fix.

Modelled on ``service/deliver.py``'s ``config_report`` -- the same idea that a
half-configured integration should say precisely what to do next rather than
failing on the first real payment. It never returns a secret: every check
reports presence and shape, and the live check reports what Stripe said, not
what was sent.

Stripe DOES have a click-to-allow flow for your own account, and an earlier
version of this file said flatly that it did not. The correction, verified
live against Stripe's own metadata endpoints:

* ``https://mcp.stripe.com/.well-known/oauth-protected-resource`` points at
  the authorization server ``https://access.stripe.com/mcp``, which advertises
  Dynamic Client Registration, PKCE S256, and
  ``token_endpoint_auth_methods_supported: ["none"]`` -- a public/native
  client with **no client secret to ship**. A native client registered against
  it returns a client_id, and the authorize endpoint routes into the Dashboard
  login and consent screen. The owner revokes it under user settings ->
  OAuth sessions.
* That is structurally the same flow ``googleapps/auth.py`` already
  implements for Google: browser consent, loopback redirect, PKCE, refresh.

That flow is now implemented in ``billing/oauth.py`` and reachable from the
Settings pane. The catch, and it is a real one: the token's audience is the
MCP resource and the scope granted is ``mcp``, so it proves who you are and
lets the app talk to Stripe through MCP tool calls -- it does **not** hand
back an API key, and the REST paths in this package (Checkout, webhook
fulfilment, overage charges) still need a restricted key. Consent is a
shortcut past "find the dashboard", not past key setup.

Connect OAuth is genuinely the wrong tool here -- its documented response now
carries only ``stripe_user_id``, with access_token and refresh_token marked
deprecated in favour of "use the Stripe-Account header with your platform's
secret key", so the credential that actually authorizes is a platform secret
this app cannot hold. Stripe Apps OAuth needs marketplace publication and a
vendor-run token exchange.

For the key itself, these remain the low-friction routes:

* ``stripe sandbox create`` -- working test keys with no registration at all;
* ``stripe login`` -- the CLI's browser pairing, the closest thing to the
  click-allow popup people expect.

Both are printed as copyable commands, so "set it up" is a paste, not a hunt
through a dashboard.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from .transport import HttpRequest, Transport, mask_key, urllib_transport

__all__ = ["Gap", "setup_report", "verify_key"]

#: The three things without which nothing works, in the order to do them.
_REQUIRED = (
    (
        "STRIPE_API_KEY",
        "A restricted key (rk_), not a secret key",
        "Dashboard -> Developers -> API keys -> Create restricted key. "
        "Give it exactly two permissions: Checkout Sessions write, "
        "PaymentIntents write. Or run `stripe sandbox create` for test keys "
        "with no registration.",
    ),
    (
        "STRIPE_WEBHOOK_SECRET",
        "The signing secret for THIS endpoint",
        "Dashboard -> Developers -> Webhooks -> your endpoint -> Signing "
        "secret. Locally, `stripe listen --forward-to "
        "localhost:8081/billing/webhook` prints one for the session. It is "
        "per-endpoint; another endpoint's secret fails every check.",
    ),
    (
        "STRIPE_PRICE_ID",
        "The price of one credit pack",
        "Dashboard -> Product catalogue -> add a one-time price. Copy the "
        "price_... id, not the prod_... id.",
    ),
)


@dataclass(frozen=True)
class Gap:
    key: str
    what: str
    fix: str
    present: bool


def _shape_problem(name: str, value: str) -> str | None:
    """A present-but-wrong value is worse than a missing one: it starts."""
    if name == "STRIPE_API_KEY":
        if value.startswith("pk_"):
            return (
                "That is a publishable key. It is safe to share and cannot "
                "create a Checkout Session. You want the restricted (rk_) key."
            )
        if not value.startswith(("rk_", "sk_")):
            return "A Stripe API key starts with rk_ (restricted) or sk_ (secret)."
        if value.startswith("sk_"):
            return (
                "This works, but it is a full secret key. A restricted key "
                "(rk_) with only Checkout and PaymentIntents write can do far "
                "less damage if it leaks."
            )
    if name == "STRIPE_WEBHOOK_SECRET" and not value.startswith("whsec_"):
        return "A webhook signing secret starts with whsec_."
    if name == "STRIPE_PRICE_ID":
        if value.startswith("prod_"):
            return "That is a product id. You need the price id under it (price_...)."
        if not value.startswith("price_"):
            return "A price id starts with price_."
    return None


def setup_report(env: dict[str, str] | None = None) -> dict[str, Any]:
    """Everything the setup screen needs, and nothing secret."""
    src = os.environ if env is None else env
    steps: list[dict[str, Any]] = []
    for name, what, fix in _REQUIRED:
        value = (src.get(name) or "").strip()
        problem = _shape_problem(name, value) if value else None
        steps.append(
            {
                "key": name,
                "what": what,
                "fix": fix,
                "present": bool(value),
                # Shown so someone can tell WHICH key is installed without
                # revealing it -- the prefix says test vs live at a glance.
                "preview": mask_key(value) if value else None,
                "warning": problem,
            }
        )
    ready = all(s["present"] and not s["warning"] for s in steps)
    mode = "unknown"
    api_key = (src.get("STRIPE_API_KEY") or "").strip()
    if api_key:
        mode = "live" if "_live_" in api_key else "test"
    return {
        "ready": ready,
        "mode": mode,
        "steps": steps,
        "quickstart": [
            {
                "label": "No Stripe account yet",
                "command": "stripe sandbox create",
                "note": "Generates working test keys with no registration.",
            },
            {
                "label": "Pair an existing account",
                "command": "stripe login",
                "note": "Opens the browser and asks you to allow access.",
            },
            {
                "label": "Receive webhooks locally",
                "command": "stripe listen --forward-to localhost:8081/billing/webhook",
                "note": "Prints the signing secret to use for this session.",
            },
        ],
        # The consent flow is real and is now wired up (``billing/oauth.py``,
        # POST /billing/connect). What it is NOT is a way to skip the key: it
        # grants the ``mcp`` scope, and everything below runs on REST.
        "oauth_available": True,
        "oauth_note": (
            "You can connect Stripe from the browser instead of pasting a "
            "key -- Settings -> Billing -> Connect Stripe opens Stripe's own "
            "consent page, with no client secret and PKCE S256, the same "
            "shape as this app's Google sign-in. It grants the 'mcp' scope "
            "for https://mcp.stripe.com, so it proves who you are but does "
            "not stand in for the restricted key that Checkout, webhooks and "
            "overage charges use. Revoke it in the Stripe Dashboard under "
            "user settings -> OAuth sessions."
        ),
        "oauth": {
            "start": "POST /billing/connect",
            "status": "GET /billing/connect",
            "disconnect": "POST /billing/disconnect",
            "authorization_server": "https://access.stripe.com/mcp",
            "resource": "https://mcp.stripe.com",
            "scope": "mcp",
            "pkce": "S256",
            "client_secret_required": False,
            "replaces_api_key": False,
        },
    }


def verify_key(
    api_key: str,
    *,
    price_id: str | None = None,
    api_base: str = "https://api.stripe.com",
    api_version: str = "2026-07-29.dahlia",
    transport: Transport = urllib_transport,
) -> dict[str, Any]:
    """One live call to prove the key works, without creating anything.

    Checks the price when there is one, because that proves the key *and* the
    price in the same round trip -- and a wrong price id is the failure that
    otherwise only appears at the moment a customer tries to pay.
    """
    if not api_key:
        return {"ok": False, "reason": "no key supplied"}
    path = f"/v1/prices/{price_id}" if price_id else "/v1/checkout/sessions?limit=1"
    try:
        response = transport(
            HttpRequest(
                url=f"{api_base}{path}",
                method="GET",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Stripe-Version": api_version,
                },
            )
        )
    except Exception as exc:  # noqa: BLE001 - the reason is the product here
        return {"ok": False, "reason": f"could not reach Stripe: {type(exc).__name__}"}

    body = response.json() if response.body else {}
    if response.status == 200:
        return {
            "ok": True,
            "mode": "live" if "_live_" in api_key else "test",
            "livemode": bool(body.get("livemode")) if isinstance(body, dict) else None,
            "price_active": body.get("active") if price_id else None,
            "currency": body.get("currency") if price_id else None,
            "unit_amount": body.get("unit_amount") if price_id else None,
        }

    err = body.get("error", {}) if isinstance(body, dict) else {}
    code = err.get("code") or err.get("type") or ""
    if response.status == 401:
        reason = "Stripe rejected the key. Check it was copied whole."
    elif response.status == 403:
        reason = (
            "The key is valid but lacks a permission. A restricted key needs "
            "Checkout Sessions write and PaymentIntents write."
        )
    elif response.status == 404 and price_id:
        reason = (
            f"The key works, but no price {price_id} exists in this account. "
            "Check test vs live mode -- a test key cannot see live prices."
        )
    else:
        reason = err.get("message") or f"Stripe answered {response.status}"
    return {"ok": False, "reason": reason, "code": code, "status": response.status}
