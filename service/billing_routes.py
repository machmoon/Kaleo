"""The HTTP surface for ``billing/``: one webhook, one checkout, one balance.

Structured like ``service/deliver.py`` -- the billing package is imported
lazily and every outbound call goes through a module-level seam, so the
service still starts, still serves, and still tests with no Stripe
configuration at all.

The one thing this module exists to get right:

    **The webhook body is verified as raw bytes, before it is parsed.**

``Handler._read_payload`` cannot be used here. It reads and JSON-decodes in one
step, and a signature checked against a re-serialised object verifies a
different string than the one Stripe signed. So this route reads the raw body
itself and hands those exact bytes to ``billing.verify_signature``.

The status codes are load-bearing, because Stripe retries any non-2xx for
days:

* forged or stale signature -> **400**, and nothing is parsed;
* replayed event -> **200**, it was already applied and retrying is pointless;
* completed-but-unpaid -> **200**, the async success event will follow;
* unhandled event type -> **200**, silence is the correct answer;
* our own failure -> **500**, so Stripe *does* retry and the payment is not lost.

Answering 500 to a replay, or 200 to our own crash, are the two ways this
route can lose money. Both are tested.
"""

from __future__ import annotations

import os
import sys
import threading
import uuid
from collections.abc import Callable
from typing import Any

from . import envfiles as _envfiles

__all__ = [
    "ENV_WINS_NOTE",
    "MAX_WEBHOOK_BYTES",
    "billing_env_path",
    "handle_config_get",
    "handle_config_post",
    "handle_connect_start",
    "handle_connect_status",
    "handle_disconnect",
    "billing_enabled",
    "config_factory",
    "handle_balance",
    "handle_checkout",
    "handle_webhook",
    "ledger_factory",
    "load_saved_env",
    "BILLING_LEDGER_PATH",
    "reset_for_tests",
    "verify_transport_factory",
]

#: Stripe events are small; a megabyte is already generous. Bounded before the
#: read so a hostile Content-Length cannot make the service allocate.
MAX_WEBHOOK_BYTES = 1 << 20


def _default_config():
    from billing.config import BillingConfig

    return BillingConfig.from_env()


#: Where the durable ledger lives. Overridable for tests and for a deployment
#: that puts state somewhere other than the home directory.
BILLING_LEDGER_PATH = os.environ.get("KALEO_LEDGER_PATH", "").strip()


def _default_ledger():
    """A ledger on disk, falling back to memory only if the file is unusable.

    Durability is not a nicety here. Stripe retries a webhook for days, so a
    restart between the first delivery and the retry used to mean the replay
    guard had forgotten the event and the same Checkout Session granted the
    pack twice.

    The fallback exists because a desktop app that cannot open its state file
    -- a read-only home directory, a full disk -- must still start; billing
    then degrades to the in-process stand-in, and the failure is written to
    stderr rather than swallowed, because "your balance resets on restart" is
    something the operator has to know.
    """
    from billing.sqlite_ledger import SqliteLedger, default_ledger_path

    path = BILLING_LEDGER_PATH or default_ledger_path()
    try:
        return SqliteLedger(path)
    except Exception as exc:  # noqa: BLE001 - reported, then degraded
        from billing.ledger import MemoryLedger

        sys.stderr.write(
            f"billing: could not open the ledger at {path} "
            f"({type(exc).__name__}); falling back to an in-memory ledger, "
            "so balances will not survive a restart\n"
        )
        return MemoryLedger()


#: Seams, the ``deliver.transport_factory`` convention. Tests replace these.
config_factory: Callable[[], Any] = _default_config
ledger_factory: Callable[[], Any] = _default_ledger

_LEDGER: Any = None


def _ledger() -> Any:
    """The process's ledger, opened once.

    Durable since `SqliteLedger` landed, which removes the in-memory
    stand-in's worst property -- a forgotten idempotency key after a restart.
    It does *not* by itself make this safe to run as two replicas: SQLite on a
    shared filesystem is not a distributed database, so multi-instance is
    still a real deployment gate. What the file does guarantee is that the
    replay constraint holds across two processes on one machine, which is the
    case that actually happens (the app plus `stripe listen`).
    """
    global _LEDGER
    if _LEDGER is None:
        _LEDGER = ledger_factory()
    return _LEDGER


def reset_for_tests() -> None:
    global _LEDGER
    _LEDGER = None


def billing_enabled() -> bool:
    """True when the environment carries enough to run billing at all."""
    return all(
        os.environ.get(name, "").strip()
        for name in ("STRIPE_API_KEY", "STRIPE_WEBHOOK_SECRET", "STRIPE_PRICE_ID")
    )


#: Where a desktop install keeps its Stripe configuration.
#:
#: This path exists for the LOCAL, single-user desktop case, where the service
#: runs on 127.0.0.1 and there is no secrets vault to reach for. It is written
#: 0600 by ``service.envfiles`` and is never the right answer for the deployed
#: Cloud Run service -- that one reads Secret Manager, and the setup screen
#: says so. Resolved at call time (``KALEO_BILLING_ENV_PATH``, else
#: ``$KALEO_HOME/billing.env``), never at import, so a test's ``KALEO_HOME``
#: is honoured and the developer's real file is never touched.
def billing_env_path() -> str:
    return str(_envfiles.env_path("billing"))


_SETTABLE = _envfiles.BILLING_KEYS


def _default_verify_transport():
    from billing.transport import urllib_transport

    return urllib_transport


#: The seam ``handle_config_post`` verifies a key through when the caller
#: passes no transport. Tests and ``service.setup``'s demo mode replace it;
#: the default is the real ``urllib`` transport, host-allowlisted to Stripe.
verify_transport_factory: Callable[[], Any] = _default_verify_transport


def load_saved_env(path: str | None = None) -> int:
    """Load a previously saved local configuration into this process.

    Called from ``service.app.main`` and the desktop launcher (never at
    import) so a desktop install that was set up yesterday is still set up
    today. Never overwrites a value already in the environment: a real
    deployment's Secret Manager injection must always win over a file left
    behind on a laptop. A file carrying the demo marker is refused by
    ``envfiles.load_env`` and counts as nothing loaded.
    """
    target = path or billing_env_path()
    try:
        values = _envfiles.load_env(target)
    except (OSError, _envfiles.EnvFileError):
        return 0
    return _envfiles.apply_env(values, allow=_SETTABLE)


#: The sentence a save carries when the running process is not using it.
ENV_WINS_NOTE = (
    "Saved and verified, but the running engine still uses the value from "
    "its environment; restart it or remove that variable."
)


def handle_config_get() -> tuple[int, dict[str, Any]]:
    """What is configured, what is missing, and the exact fix for each gap."""
    from billing.setup import setup_report

    report = setup_report()
    path = billing_env_path()
    report["storage"] = {
        "path": path,
        "note": (
            "Local desktop storage, written 0600. A deployed service should "
            "use its platform's secrets vault instead; values already in the "
            "environment always win over this file."
        ),
        "mode_hint": _envfiles.mode_hint(path),
    }
    return 200, report


def handle_config_post(
    payload: dict[str, Any],
    *,
    transport: Any | None = None,
    env_path: str | None = None,
    apply_env: bool = True,
) -> tuple[int, dict[str, Any]]:
    """Verify a key against Stripe, then save it. Verify first, always.

    Saving an unverified key is how someone ends up believing billing is
    configured until the first customer tries to pay.

    ``transport`` overrides :data:`verify_transport_factory` for this call,
    ``env_path`` overrides :func:`billing_env_path`, and ``apply_env=False``
    saves without touching ``os.environ`` (``service.setup`` uses all three).
    The response says whether the running process is now using the saved
    values (``active`` / ``active_source``): with ``setdefault`` semantics a
    variable already exported wins, and a save that silently lost to it
    would look like a save that worked.
    """
    from billing.setup import setup_report, verify_key

    values = {k: str(payload.get(k, "")).strip() for k in _SETTABLE}
    api_key = values["STRIPE_API_KEY"]
    price_id = values["STRIPE_PRICE_ID"] or None

    if payload.get("verify_only") or api_key:
        check = verify_key(
            api_key,
            price_id=price_id,
            transport=transport or verify_transport_factory(),
        )
        if not check.get("ok"):
            return 400, {"saved": False, "check": check}
    else:
        check = {"ok": False, "reason": "no key supplied"}

    if payload.get("verify_only"):
        return 200, {"saved": False, "check": check}

    target = env_path or billing_env_path()
    try:
        _envfiles.save_env(target, values, allow=_SETTABLE)
    except _envfiles.EnvFileError as exc:
        return 400, {"saved": False, "error": f"could not write config: {exc}"}
    except OSError as exc:
        return 500, {"saved": False, "error": f"could not write config: {exc.strerror}"}

    body: dict[str, Any] = {"saved": True, "check": check}
    if apply_env:
        _envfiles.apply_env(values, allow=_SETTABLE)
        active, source = _envfiles.activation(values)
        body["active"], body["active_source"] = active, source
        if not active:
            body["note"] = ENV_WINS_NOTE
    else:
        body["active"], body["active_source"] = False, "file"
    body["report"] = setup_report()
    return 200, body


# -- the browser consent flow ---------------------------------------------
#
# Stripe publishes a real OAuth authorization server for its MCP resource
# (``access.stripe.com/mcp``): dynamic client registration, PKCE S256, and no
# client secret. That is the "click allow" popup, the same shape as this
# repo's Google sign-in. What it grants is the ``mcp`` scope, *not* an API
# key, so it complements ``handle_config_post`` rather than replacing it --
# see ``billing.oauth.describe``.

_CONNECT: dict[str, Any] = {"state": "idle"}
_CONNECT_LOCK = threading.Lock()


def _connect_worker() -> None:
    from billing import oauth

    def on_url(url: str) -> None:
        with _CONNECT_LOCK:
            _CONNECT["url"] = url

    try:
        result = oauth.run_oauth_flow(on_url=on_url)
    except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
        with _CONNECT_LOCK:
            # The message is Stripe's or ours; neither carries a token, and
            # the verifier never left ``run_oauth_flow``'s frame.
            _CONNECT.update(state="failed", error=str(exc), url=None)
        return
    with _CONNECT_LOCK:
        _CONNECT.update(state="connected", result=result, error=None, url=None)


def handle_connect_start() -> tuple[int, dict[str, Any]]:
    """Open Stripe's consent page and return immediately.

    The flow blocks for up to five minutes waiting for a human to click
    Allow, so it cannot run on the request thread -- the UI polls
    ``GET /billing/connect`` for the outcome. One at a time: two concurrent
    flows would race for the same loopback port and the second would fail
    with a confusing bind error.
    """
    with _CONNECT_LOCK:
        if _CONNECT.get("state") == "running":
            return 409, {"state": "running", "url": _CONNECT.get("url")}
        _CONNECT.clear()
        _CONNECT.update(state="running", url=None, error=None)
    threading.Thread(
        target=_connect_worker, name="stripe-oauth", daemon=True
    ).start()
    return 202, {"state": "running"}


def handle_connect_status() -> tuple[int, dict[str, Any]]:
    """Where the consent flow got to, plus what consent does and does not buy.

    Never returns a token; ``oauth.describe`` is local-only and reads no
    secret out of the stored file.
    """
    from billing import oauth

    body = dict(oauth.describe())
    with _CONNECT_LOCK:
        body["flow"] = {
            "state": _CONNECT.get("state", "idle"),
            "url": _CONNECT.get("url"),
            "error": _CONNECT.get("error"),
        }
    return 200, body


def handle_disconnect() -> tuple[int, dict[str, Any]]:
    """Drop the consent. The local token goes even if Stripe is unreachable."""
    from billing import oauth

    told = oauth.revoke()
    with _CONNECT_LOCK:
        _CONNECT.clear()
        _CONNECT.update(state="idle")
    return 200, {"disconnected": True, "stripe_notified": told}


def _oops(exc: BaseException, where: str) -> tuple[int, dict[str, Any]]:
    """A 500 with an id, never the exception text: it can carry a key."""
    error_id = uuid.uuid4().hex[:12]
    sys.stderr.write(
        f"error {error_id}: billing {where} failed: {type(exc).__name__}\n"
    )
    return 500, {"error": "internal error", "error_id": error_id}


def handle_webhook(
    raw_body: bytes, signature_header: str
) -> tuple[int, dict[str, Any]]:
    """Verify, then apply, one Stripe event. ``raw_body`` must be the bytes."""
    from billing.errors import (
        BillingError,
        LedgerError,
        WebhookSignatureError,
    )
    from billing.fulfillment import handle_event
    from billing.webhook import verify_signature

    try:
        config = config_factory()
    except Exception as exc:
        return _oops(exc, "config")

    try:
        verify_signature(raw_body, signature_header, config.webhook_secret)
    except WebhookSignatureError:
        # Deliberately not echoing which check failed.
        return 400, {"error": "signature verification failed"}

    import json

    try:
        event = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        # Signed by us and still unparseable: not a retry candidate.
        return 400, {"error": "event body is not JSON"}
    if not isinstance(event, dict):
        return 400, {"error": "event body must be an object"}

    try:
        outcome = handle_event(event, _ledger())
    except LedgerError as exc:
        # A signed event we cannot apply is our problem to look at, but
        # retrying will not fix it -- 400 stops Stripe's backoff.
        sys.stderr.write(f"billing: unapplicable event {event.get('id')}: {exc}\n")
        return 400, {"error": "event could not be applied"}
    except BillingError as exc:
        return _oops(exc, "fulfillment")

    return 200, {"action": outcome.action, "granted_mkcu": outcome.granted_mkcu}


def handle_checkout(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Start a Checkout Session for one or more credit packs."""
    from billing.accounts import AccountId, SingleAccountResolver
    from billing.errors import BillingError, ConfigError, StripeError
    from billing.stripe_api import create_checkout_session

    try:
        config = config_factory()
    except ConfigError as exc:
        return 503, {"error": str(exc)}
    except Exception as exc:
        return _oops(exc, "config")

    quantity = payload.get("quantity", 1)
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 1:
        return 400, {"error": "quantity must be a positive integer"}
    if quantity > 20:
        return 400, {"error": "quantity above 20; buy a larger pack instead"}

    subject = payload.get("account")
    try:
        if subject:
            account = AccountId(subject)
        else:
            account = SingleAccountResolver().resolve(subject=None)
    except ConfigError as exc:
        return 400, {"error": str(exc)}

    try:
        session = create_checkout_session(
            config,
            account,
            quantity=quantity,
            # Keyed on the caller's own token so a double-click reuses the
            # session rather than opening a second one. A random key here
            # would defeat the point of sending one at all.
            idempotency_key=str(payload.get("idempotency_key") or "") or None,
        )
    except StripeError as exc:
        return 502, {"error": exc.args[0], "code": exc.code}
    except BillingError as exc:
        return _oops(exc, "checkout")

    return 200, {"id": session.get("id"), "url": session.get("url")}


def handle_balance(account_value: str | None) -> tuple[int, dict[str, Any]]:
    """Report spendable compute. Never touches Stripe."""
    from billing.accounts import AccountId, SingleAccountResolver
    from billing.errors import ConfigError
    from billing.units import mkcu_to_display

    try:
        account = (
            AccountId(account_value)
            if account_value
            else SingleAccountResolver().resolve(subject=None)
        )
    except ConfigError as exc:
        return 400, {"error": str(exc)}

    ledger = _ledger()
    balance = ledger.balance(account)
    return 200, {
        "account": str(account),
        "available_mkcu": balance.available_mkcu,
        "held_mkcu": balance.held_mkcu,
        "spendable_mkcu": balance.spendable_mkcu,
        "overage_mkcu": ledger.overage_mkcu(account),
        "display": mkcu_to_display(balance.spendable_mkcu),
    }
