"""Request construction, which is the half a mocked SDK never checks."""

from __future__ import annotations

import pytest

from billing.accounts import AccountId
from billing.config import BillingConfig
from billing.errors import ConfigError, StripeError
from billing.stripe_api import (
    create_checkout_session,
    retrieve_session,
)
from billing.transport import ensure_stripe_url, form_encode, mask_key

ACC = AccountId("local")


def test_checkout_posts_form_encoded_not_json(config, transport):
    create_checkout_session(config, ACC, transport=transport)
    assert transport.last.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert transport.last.method == "POST"
    assert transport.last.url.endswith("/v1/checkout/sessions")


def test_payment_method_types_is_never_sent(config, transport):
    """Sending it disables dynamic payment methods. Stripe names this as the
    trap; hardcoding ['card'] is a conversion bug."""
    create_checkout_session(config, ACC, transport=transport)
    assert not any(k.startswith("payment_method_types") for k in transport.form())


def test_the_api_version_is_pinned(config, transport):
    create_checkout_session(config, ACC, transport=transport)
    assert transport.last.headers["Stripe-Version"] == config.api_version


def test_the_account_and_credit_travel_in_metadata(config, transport):
    create_checkout_session(config, ACC, transport=transport)
    form = transport.form()
    assert form["client_reference_id"] == "local"
    assert form["metadata[kaleo_account]"] == "local"
    assert form["metadata[kaleo_credit_mkcu]"] == "8888"


def test_quantity_multiplies_the_credit_granted(config, transport):
    create_checkout_session(config, ACC, quantity=3, transport=transport)
    assert transport.form()["metadata[kaleo_credit_mkcu]"] == str(8888 * 3)
    assert transport.form()["line_items[0][quantity]"] == "3"


def test_an_idempotency_key_is_forwarded(config, transport):
    create_checkout_session(config, ACC, transport=transport, idempotency_key="abc123")
    assert transport.last.headers["Idempotency-Key"] == "abc123"


def test_integration_identifier_carries_an_eight_letter_suffix(config, transport):
    create_checkout_session(config, ACC, transport=transport)
    label = transport.form()["integration_identifier"]
    suffix = label.rsplit("-", 1)[1]
    assert len(suffix) == 8 and suffix.isalpha()


def test_none_values_are_dropped_not_stringified(config, transport):
    """`customer_email=None` must not reach Stripe as the string 'None',
    which Stripe would accept and store."""
    create_checkout_session(config, ACC, transport=transport)
    assert "customer_email" not in transport.form()


def test_a_stripe_error_body_becomes_a_typed_error(config):
    from billing.tests.conftest import RecordingTransport

    t = RecordingTransport([(402, {"error": {"message": "Your card was declined.",
                                             "code": "card_declined"}})])
    with pytest.raises(StripeError) as exc:
        create_checkout_session(config, ACC, transport=t)
    assert exc.value.status == 402
    assert exc.value.code == "card_declined"


def test_quantity_below_one_is_refused(config, transport):
    with pytest.raises(StripeError):
        create_checkout_session(config, ACC, quantity=0, transport=transport)


def test_retrieve_rejects_an_id_that_is_not_a_session(config, transport):
    with pytest.raises(StripeError):
        retrieve_session(config, "pi_123", transport=transport)


@pytest.mark.parametrize(
    "url",
    [
        "https://api.stripe.com.evil.example/v1/x",  # suffix attack
        "http://api.stripe.com/v1/x",  # downgraded to http
        "https://evil.example/v1/x",
    ],
)
def test_the_host_allowlist_holds_for_every_transport(url):
    """Enforced at construction, so it applies to the fakes too."""
    with pytest.raises(StripeError):
        ensure_stripe_url(url)


def test_form_encoding_nests_the_way_stripe_expects():
    encoded = form_encode(
        {"line_items": [{"price": "price_1", "quantity": 2}], "on": True}
    )
    assert b"line_items%5B0%5D%5Bprice%5D=price_1" in encoded
    assert b"on=true" in encoded  # lower-case, not Python's "True"


def test_a_key_is_never_reproduced_in_full():
    # Deliberately shorter than the key-shaped pattern the pre-commit hook
    # scans for, so the fixture itself never trips the scanner.
    masked = mask_key("rk_test_NOTREALxyzw")
    assert "NOTREAL" not in masked
    assert masked.startswith("rk_test_")


def test_config_describe_leaks_no_secret(config):
    described = config.describe()
    assert config.api_key not in str(described)
    assert config.webhook_secret not in str(described)
    assert described["mode"] == "test"
    assert described["key_kind"] == "restricted"


@pytest.mark.parametrize(
    "field,value",
    [
        ("api_key", "nonsense"),
        ("webhook_secret", "notasecret"),
        ("price_id", "prod_123"),
        ("credit_mkcu_per_purchase", 0),
    ],
)
def test_config_refuses_to_start_half_configured(config, field, value):
    kwargs = {
        "api_key": config.api_key,
        "webhook_secret": config.webhook_secret,
        "price_id": config.price_id,
        "credit_mkcu_per_purchase": config.credit_mkcu_per_purchase,
        "success_url": config.success_url,
        "cancel_url": config.cancel_url,
    }
    kwargs[field] = value
    with pytest.raises(ConfigError):
        BillingConfig(**kwargs)


def test_from_env_names_what_is_missing():
    with pytest.raises(ConfigError) as exc:
        BillingConfig.from_env({"STRIPE_API_KEY": "rk_test_x"})
    assert "STRIPE_WEBHOOK_SECRET" in str(exc.value)
    assert "STRIPE_PRICE_ID" in str(exc.value)


# ------------------------------------------------------------ overage calls
from billing.stripe_api import charge_overage, create_setup_session  # noqa: E402


def test_setup_mode_charges_nothing_now(config, transport):
    create_setup_session(config, ACC, transport=transport)
    form = transport.form()
    assert form["mode"] == "setup"
    assert not any(k.startswith("line_items") for k in form)


def test_an_off_session_charge_is_confirmed_and_flagged(config, transport):
    charge_overage(
        config, ACC, amount_cents=675, customer="cus_1", payment_method="pm_1",
        transport=transport, idempotency_key="settle-2026-09",
    )
    form = transport.form()
    assert transport.last.url.endswith("/v1/payment_intents")
    assert form["off_session"] == "true"
    assert form["confirm"] == "true"
    assert form["amount"] == "675"
    assert transport.last.headers["Idempotency-Key"] == "settle-2026-09"


def test_an_overage_charge_never_sends_payment_method_types(config, transport):
    charge_overage(config, ACC, amount_cents=675, customer="cus_1",
                   payment_method="pm_1", transport=transport)
    assert not any(k.startswith("payment_method_types") for k in transport.form())


def test_a_non_positive_overage_charge_is_refused(config, transport):
    for amount in (0, -100):
        with pytest.raises(StripeError):
            charge_overage(config, ACC, amount_cents=amount, customer="cus_1",
                           payment_method="pm_1", transport=transport)


def test_authentication_required_surfaces_as_a_typed_error(config):
    """The bank wants the customer. Not a retry, and not a reason to claw back
    compute that was already delivered."""
    from billing.tests.conftest import RecordingTransport

    t = RecordingTransport(
        [(402, {"error": {"code": "authentication_required", "message": "auth"}})]
    )
    with pytest.raises(StripeError) as exc:
        charge_overage(config, ACC, amount_cents=675, customer="cus_1",
                       payment_method="pm_1", transport=t)
    assert exc.value.code == "authentication_required"


# ------------------------------------------------- hardening (review fixes)
from billing.transport import HttpRequest  # noqa: E402


def test_a_request_repr_redacts_the_bearer_token():
    """This object sits in the frame of every transport failure."""
    r = HttpRequest(
        url="https://api.stripe.com/v1/x",
        headers={"Authorization": "Bearer rk_live_ABCDEFGHIJKLMNOP"},
    )
    assert "ABCDEFGHIJKLMNOP" not in repr(r)
    assert "rk_live_" in repr(r)  # the prefix is the part worth seeing


def test_a_config_repr_redacts_both_secrets(config):
    """describe() honoured 'never echoed'; the dataclass __repr__ did not."""
    assert config.api_key not in repr(config)
    assert config.webhook_secret not in repr(config)
    assert config.api_key not in str(config)


def test_the_allowlist_holds_at_construction_not_just_in_the_transport():
    """The docstring claimed this; only urllib_transport actually checked, so
    a fake transport would have happily carried the key off-allowlist."""
    with pytest.raises(StripeError):
        HttpRequest(url="https://evil.example/v1/checkout/sessions")


def test_a_config_cannot_point_the_api_base_elsewhere(config):
    with pytest.raises(StripeError):
        BillingConfig(
            api_key=config.api_key,
            webhook_secret=config.webhook_secret,
            price_id=config.price_id,
            credit_mkcu_per_purchase=1,
            success_url=config.success_url,
            cancel_url=config.cancel_url,
            api_base="https://evil.example",
        )


@pytest.mark.parametrize(
    "session_id",
    [
        "cs_a/../../../../v1/customers?limit=100",  # path + query smuggling
        "cs_a?expand[]=customer",
        "cs_a/refund",
        "cs_",
        "cs_a b",
    ],
)
def test_a_session_id_is_matched_whole_not_by_prefix(config, transport, session_id):
    """The host allowlist cannot catch this -- the host really is Stripe."""
    with pytest.raises(StripeError):
        retrieve_session(config, session_id, transport=transport)


def test_setup_mode_sends_a_currency(config, transport):
    """Required when payment_method_types is unset, which it always is here.
    Without it the call 400s and no card can ever be saved for overage."""
    create_setup_session(config, ACC, transport=transport)
    assert transport.form()["currency"] == "usd"
