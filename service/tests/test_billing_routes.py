"""The billing HTTP surface: raw-body verification and the status-code map.

Stripe retries any non-2xx for days, so the code this route returns *is* the
behaviour. Answering 500 to a replay loops forever; answering 200 to our own
crash loses a payment silently. Both directions are tested here.
"""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

from billing.config import BillingConfig
from billing.ledger import MemoryLedger
from billing.webhook import sign_payload
from service import billing_routes


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    ledger = MemoryLedger()
    config = BillingConfig(
        api_key="rk_test_abcd",
        webhook_secret="whsec_routetest",
        price_id="price_x",
        credit_mkcu_per_purchase=8888,
        success_url="kaleo://billing/success",
        cancel_url="kaleo://billing/cancel",
    )
    monkeypatch.setattr(billing_routes, "config_factory", lambda: config)
    monkeypatch.setattr(billing_routes, "ledger_factory", lambda: ledger)
    billing_routes.reset_for_tests()
    yield ledger
    billing_routes.reset_for_tests()


def paid_event(event_id="evt_1", status="paid", kind="checkout.session.completed"):
    return {
        "id": event_id,
        "type": kind,
        "data": {
            "object": {
                "id": "cs_1",
                "mode": "payment",
                "payment_status": status,
                "amount_total": 2000,
                "client_reference_id": "local",
                "metadata": {"kaleo_account": "local", "kaleo_credit_mkcu": "8888"},
            }
        },
    }


def signed(event):
    raw = json.dumps(event).encode()
    return raw, sign_payload(raw, "whsec_routetest", timestamp=int(time.time()))


def test_a_genuine_paid_event_grants_and_answers_200(fresh):
    raw, sig = signed(paid_event())
    status, body = billing_routes.handle_webhook(raw, sig)
    assert status == 200
    assert body["granted_mkcu"] == 8888


def test_a_replay_answers_200_so_stripe_stops_retrying(fresh):
    """A non-2xx here makes Stripe retry the same event for days."""
    raw, sig = signed(paid_event())
    billing_routes.handle_webhook(raw, sig)
    status, body = billing_routes.handle_webhook(raw, sig)
    assert status == 200
    assert body["action"] == "replayed"
    assert body["granted_mkcu"] == 0


def test_a_forged_signature_is_400_and_grants_nothing(fresh):
    raw, _ = signed(paid_event())
    status, body = billing_routes.handle_webhook(raw, "t=1,v1=deadbeef")
    assert status == 400
    assert fresh.entries() == []


def test_a_tampered_body_is_rejected_even_with_a_valid_header(fresh):
    """The signature covers the raw bytes; changing one byte must break it."""
    raw, sig = signed(paid_event())
    status, _ = billing_routes.handle_webhook(raw.replace(b"8888", b"9999"), sig)
    assert status == 400
    assert fresh.entries() == []


def test_re_serialising_the_body_would_break_verification(fresh):
    """Proves why this route cannot use Handler._read_payload: a parse-and-
    re-encode round trip does not reproduce the signed bytes."""
    event = paid_event()
    raw, sig = signed(event)
    reserialised = json.dumps(json.loads(raw), indent=2).encode()
    assert reserialised != raw
    assert billing_routes.handle_webhook(reserialised, sig)[0] == 400


def test_the_error_response_names_no_reason(fresh):
    """Saying which half of the check failed helps only an attacker."""
    raw, _ = signed(paid_event())
    _, body = billing_routes.handle_webhook(raw, "t=1,v1=deadbeef")
    assert body["error"] == "signature verification failed"


def test_an_unpaid_session_answers_200_and_grants_nothing(fresh):
    raw, sig = signed(paid_event(status="unpaid"))
    status, body = billing_routes.handle_webhook(raw, sig)
    assert status == 200
    assert body["action"] == "pending"
    assert fresh.entries() == []


def test_the_later_async_success_is_still_granted(fresh):
    raw, sig = signed(paid_event(event_id="evt_a", status="unpaid"))
    billing_routes.handle_webhook(raw, sig)
    raw2, sig2 = signed(
        paid_event(event_id="evt_b", kind="checkout.session.async_payment_succeeded")
    )
    status, body = billing_routes.handle_webhook(raw2, sig2)
    assert status == 200 and body["granted_mkcu"] == 8888


def test_an_unhandled_event_type_answers_200(fresh):
    raw, sig = signed(paid_event(kind="customer.created"))
    status, body = billing_routes.handle_webhook(raw, sig)
    assert status == 200 and body["action"] == "ignored"


def test_a_signed_but_unapplicable_event_is_200_not_a_retry_loop(fresh):
    """Retrying can never fix a malformed grant, so do not invite it at all.

    A non-2xx here makes Stripe retry for three days and, on repeated
    failure, can disable the endpoint -- taking real purchases down too.
    """
    event = paid_event()
    event["data"]["object"]["metadata"]["kaleo_credit_mkcu"] = "0"
    raw, sig = signed(event)
    status, body = billing_routes.handle_webhook(raw, sig)
    assert status == 200 and body["action"] == "ignored"
    assert fresh.entries() == []


def test_non_json_with_a_valid_signature_is_400(fresh):
    raw = b"not json at all"
    sig = sign_payload(raw, "whsec_routetest", timestamp=int(time.time()))
    assert billing_routes.handle_webhook(raw, sig)[0] == 400


def test_our_own_failure_is_500_so_stripe_retries(fresh, monkeypatch):
    """The payment is real; losing it because we crashed is the bad outcome."""

    def boom():
        raise RuntimeError("config exploded")

    monkeypatch.setattr(billing_routes, "config_factory", boom)
    status, body = billing_routes.handle_webhook(b"{}", "t=1,v1=x")
    assert status == 500
    assert "error_id" in body


def test_a_500_never_leaks_the_exception_text(fresh, monkeypatch):
    """An exception message can carry a key or a customer id."""

    def boom():
        raise RuntimeError("rk_test_SUPERSECRET leaked here")

    monkeypatch.setattr(billing_routes, "config_factory", boom)
    _, body = billing_routes.handle_webhook(b"{}", "t=1,v1=x")
    assert "SUPERSECRET" not in json.dumps(body)


def test_balance_reports_holds_and_overage_without_touching_stripe(fresh):
    from billing.accounts import AccountId
    from billing.ledger import OveragePolicy

    fresh.grant(AccountId("local"), 3000, reason="pack", event_id="evt_seed")
    fresh.reserve(AccountId("local"), 1000, run_id="r1")
    status, body = billing_routes.handle_balance(None)
    assert status == 200
    assert body["available_mkcu"] == 3000
    assert body["held_mkcu"] == 1000
    assert body["spendable_mkcu"] == 2000
    assert body["overage_mkcu"] == 0

    fresh.commit("r1", 5000)
    _, body = billing_routes.handle_balance(None)
    assert body["overage_mkcu"] == 2000
    assert OveragePolicy().enabled


def test_a_malformed_account_is_400_not_500(fresh):
    status, _ = billing_routes.handle_balance("../../etc/passwd")
    assert status == 400


@pytest.mark.parametrize("quantity", [0, -1, "3", 3.5, True, 21])
def test_checkout_refuses_a_bad_quantity(fresh, quantity):
    status, _ = billing_routes.handle_checkout({"quantity": quantity})
    assert status == 400


def test_checkout_without_configuration_is_503_not_500(monkeypatch):
    from billing.errors import ConfigError

    def unconfigured():
        raise ConfigError("missing required environment: STRIPE_API_KEY")

    monkeypatch.setattr(billing_routes, "config_factory", unconfigured)
    status, body = billing_routes.handle_checkout({"quantity": 1})
    assert status == 503
    assert "STRIPE_API_KEY" in body["error"]


# -- the browser consent flow ----------------------------------------------
#
# Stripe's consent is a real popup, but it grants MCP scope rather than an API
# key. These pin both halves: that the flow works without blocking a request
# thread, and that the UI is never told consent replaced key setup.


@pytest.fixture
def consent(tmp_path, monkeypatch):
    """Point the OAuth module's token file at tmp and reset flow state."""
    from billing import oauth

    path = tmp_path / "stripe_oauth.json"
    monkeypatch.setattr(oauth, "TOKEN_PATH", path)
    billing_routes._CONNECT.clear()
    billing_routes._CONNECT.update(state="idle")
    yield path
    billing_routes._CONNECT.clear()
    billing_routes._CONNECT.update(state="idle")


def test_connect_status_reports_not_connected_before_anyone_clicks(consent):
    status, body = billing_routes.handle_connect_status()
    assert status == 200
    assert body["connected"] is False
    assert body["flow"]["state"] == "idle"


def test_connect_status_says_consent_does_not_replace_the_api_key(consent):
    """The claim that shipped before this feature was that Stripe had no
    consent flow at all. The correction must not overshoot into implying
    consent is all you need."""
    _, body = billing_routes.handle_connect_status()
    assert "restricted key" in body["does_not_cover"]
    assert body["client_secret_required"] is False


def test_connect_start_answers_immediately_and_does_not_block(consent, monkeypatch):
    """The flow waits up to five minutes for a human. If it ran on the
    request thread the whole service would stall behind one consent page."""
    from billing import oauth

    released = threading.Event()

    def slow_flow(*args, on_url=None, **kwargs):
        if on_url:
            on_url("https://access.stripe.com/mcp/oauth2/authorize?x=1")
        released.wait(5)
        return {"connected": True, "scope": "mcp"}

    monkeypatch.setattr(oauth, "run_oauth_flow", slow_flow)
    status, body = billing_routes.handle_connect_start()
    assert status == 202
    assert body["state"] == "running"

    for _ in range(500):
        _, seen = billing_routes.handle_connect_status()
        if seen["flow"]["url"]:
            break
        time.sleep(0.01)
    assert seen["flow"]["url"].startswith("https://access.stripe.com/")
    assert seen["flow"]["state"] == "running"

    released.set()
    for _ in range(500):
        _, seen = billing_routes.handle_connect_status()
        if seen["flow"]["state"] != "running":
            break
        time.sleep(0.01)
    assert seen["flow"]["state"] == "connected"


def test_a_second_connect_while_one_is_running_is_refused(consent, monkeypatch):
    """Two flows would race for the same loopback port and the second would
    die with a bind error the user cannot interpret."""
    from billing import oauth

    released = threading.Event()
    monkeypatch.setattr(
        oauth, "run_oauth_flow", lambda *a, **k: (released.wait(5), {})[1]
    )
    assert billing_routes.handle_connect_start()[0] == 202
    status, body = billing_routes.handle_connect_start()
    assert status == 409
    assert body["state"] == "running"
    released.set()


def test_a_failed_consent_is_reported_without_a_traceback(consent, monkeypatch):
    from billing import oauth

    def refuse(*args, **kwargs):
        raise oauth.OAuthError("Stripe refused authorization: access_denied")

    monkeypatch.setattr(oauth, "run_oauth_flow", refuse)
    billing_routes.handle_connect_start()
    for _ in range(500):
        _, seen = billing_routes.handle_connect_status()
        if seen["flow"]["state"] != "running":
            break
        time.sleep(0.01)
    assert seen["flow"]["state"] == "failed"
    assert "access_denied" in seen["flow"]["error"]
    assert seen["connected"] is False


def test_disconnect_is_safe_when_nothing_was_ever_connected(consent):
    status, body = billing_routes.handle_disconnect()
    assert status == 200
    assert body["disconnected"] is True
    assert body["stripe_notified"] is False


def test_no_connect_response_ever_carries_a_token(consent, monkeypatch):
    """The single invariant worth a test of its own: these bodies are JSON
    that reaches a renderer process, and one of them is built from a file
    that holds an access token."""
    from billing import oauth

    oauth.save_state(
        consent,
        server=oauth.AuthServer(
            issuer="https://access.stripe.com/mcp",
            authorization_endpoint="https://access.stripe.com/mcp/oauth2/authorize",
            token_endpoint="https://access.stripe.com/mcp/oauth2/token",
            registration_endpoint="https://access.stripe.com/mcp/oauth2/register",
        ),
        client=oauth.StoredClient("oacli_x", "http://127.0.0.1:53682/callback"),
        token={
            "access_token": "SENTINELTOKEN",
            "refresh_token": "SENTINELREFRESH",
            "expires_at": time.time() + 3600,
        },
    )
    _, body = billing_routes.handle_connect_status()
    assert body["connected"] is True
    blob = json.dumps(body)
    assert "SENTINEL" not in blob


# -- the durable ledger ----------------------------------------------------
#
# The autouse `fresh` fixture pins `ledger_factory` to one MemoryLedger for
# every test in this file. These two are about `_default_ledger` itself, so
# they have to put the real factory back first — without that, the restart
# test passes for the wrong reason: the fixture's closure hands back the same
# object across `reset_for_tests()`, so nothing is being persisted at all.


def real_factory(monkeypatch, path) -> None:
    monkeypatch.setattr(
        billing_routes, "ledger_factory", billing_routes._default_ledger
    )
    monkeypatch.setattr(billing_routes, "BILLING_LEDGER_PATH", str(path))
    billing_routes.reset_for_tests()


def test_the_default_ledger_is_on_disk_and_survives_a_restart(tmp_path, monkeypatch):
    """The in-memory stand-in forgot idempotency keys on restart, and Stripe
    retries for days — so the same Checkout Session granted the pack twice."""
    from billing.accounts import AccountId

    real_factory(monkeypatch, tmp_path / "ledger.sqlite3")
    account = AccountId("desk")
    billing_routes._ledger().grant(account, 4000, reason="pack", event_id="cs:once")

    # A restart: the process-local handle is dropped and reopened.
    billing_routes.reset_for_tests()
    reopened = billing_routes._ledger()
    assert type(reopened).__name__ == "SqliteLedger"  # not the fixture's stand-in
    assert reopened.balance(account).available_mkcu == 4000
    assert reopened.was_applied("cs:once") is True
    billing_routes.reset_for_tests()


def test_an_unopenable_ledger_degrades_loudly_rather_than_refusing_to_start(
    tmp_path, monkeypatch, capsys
):
    """A desktop app with a read-only home must still start. It must not do so
    quietly: "your balance resets on restart" is the operator's to know."""
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("this is a file, not a directory")
    real_factory(monkeypatch, blocked / "ledger.sqlite3")
    ledger = billing_routes._ledger()
    assert type(ledger).__name__ == "MemoryLedger"
    assert "in-memory ledger" in capsys.readouterr().err
    billing_routes.reset_for_tests()


# -- the saved configuration -----------------------------------------------
#
# ``load_saved_env`` was defined and never called: a desktop install that was
# set up yesterday forgot its Stripe key on every restart. These pin the
# envfiles-backed replacement and the seams ``service.setup`` relies on.


def stripe_answers(status=200, body=b'{"livemode": false}'):
    from billing.transport import HttpResponse

    calls = []

    def transport(request):
        calls.append(request)
        return HttpResponse(status, body)

    transport.calls = calls
    return transport


def test_config_post_verifies_through_the_injected_transport_and_saves(monkeypatch):
    from service import envfiles

    monkeypatch.delenv("STRIPE_API_KEY", raising=False)
    monkeypatch.delenv("STRIPE_PRICE_ID", raising=False)
    transport = stripe_answers()
    status, body = billing_routes.handle_config_post(
        {"STRIPE_API_KEY": "rk_test_51abcdefghijklmnop", "STRIPE_PRICE_ID": "price_9"},
        transport=transport,
    )
    assert status == 200, body
    assert body["saved"] is True and body["check"]["ok"] is True
    assert body["active"] is True and body["active_source"] == "file"
    assert [r.url for r in transport.calls] == ["https://api.stripe.com/v1/prices/price_9"]
    path = envfiles.env_path("billing")
    assert envfiles.load_env(path) == {
        "STRIPE_API_KEY": "rk_test_51abcdefghijklmnop",
        "STRIPE_PRICE_ID": "price_9",
    }
    assert "rk_test_51abcdefghijklmnop" not in json.dumps(body)


def test_config_post_uses_the_factory_seam_when_no_transport_is_given(monkeypatch):
    transport = stripe_answers(401, b'{"error": {"code": "invalid_api_key"}}')
    monkeypatch.setattr(billing_routes, "verify_transport_factory", lambda: transport)
    status, body = billing_routes.handle_config_post({"STRIPE_API_KEY": "rk_test_bad"})
    assert status == 400 and body["saved"] is False
    assert len(transport.calls) == 1


def test_config_post_apply_env_false_saves_without_touching_the_environment(
    monkeypatch, tmp_path
):
    from service import envfiles

    monkeypatch.delenv("STRIPE_API_KEY", raising=False)
    given = tmp_path / "given.env"
    status, body = billing_routes.handle_config_post(
        {"STRIPE_API_KEY": "rk_test_51abcdefghijklmnop"},
        transport=stripe_answers(),
        env_path=str(given),
        apply_env=False,
    )
    assert status == 200
    assert body["active"] is False
    assert "STRIPE_API_KEY" not in os.environ
    assert envfiles.load_env(given)["STRIPE_API_KEY"] == "rk_test_51abcdefghijklmnop"
    assert not envfiles.env_path("billing").exists()


def test_config_post_reports_when_the_environment_wins(monkeypatch):
    monkeypatch.setenv("STRIPE_API_KEY", "rk_test_fromtheshell")
    status, body = billing_routes.handle_config_post(
        {"STRIPE_API_KEY": "rk_test_51abcdefghijklmnop"}, transport=stripe_answers()
    )
    assert status == 200
    assert body["active"] is False and body["active_source"] == "environment"
    assert body["note"] == billing_routes.ENV_WINS_NOTE
    assert os.environ["STRIPE_API_KEY"] == "rk_test_fromtheshell"


def test_saved_env_is_loaded_at_startup_and_never_overrides(monkeypatch):
    """The regression: a key saved yesterday must be there after a restart."""
    from service import envfiles

    monkeypatch.delenv("STRIPE_API_KEY", raising=False)
    monkeypatch.setenv("STRIPE_PRICE_ID", "price_from_shell")
    billing_routes.handle_config_post(
        {"STRIPE_API_KEY": "rk_test_51abcdefghijklmnop", "STRIPE_PRICE_ID": "price_9"},
        transport=stripe_answers(),
        apply_env=False,
    )
    assert "STRIPE_API_KEY" not in os.environ
    assert billing_routes.load_saved_env() == 1  # the key; the price lost
    assert os.environ["STRIPE_API_KEY"] == "rk_test_51abcdefghijklmnop"
    assert os.environ["STRIPE_PRICE_ID"] == "price_from_shell"
    # A demo file at the live path is refused, not loaded.
    monkeypatch.delenv("STRIPE_API_KEY")
    path = envfiles.env_path("billing")
    path.write_text("KALEO_DEMO=1\nSTRIPE_API_KEY=rk_test_demo\n")
    assert billing_routes.load_saved_env() == 0
    assert "STRIPE_API_KEY" not in os.environ


def test_billing_env_path_is_resolved_at_call_time(monkeypatch, tmp_path):
    monkeypatch.setenv("KALEO_BILLING_ENV_PATH", str(tmp_path / "moved.env"))
    assert billing_routes.billing_env_path() == str(tmp_path / "moved.env")
    _, report = billing_routes.handle_config_get()
    assert report["storage"]["path"] == str(tmp_path / "moved.env")
    assert report["storage"]["mode_hint"] is None
