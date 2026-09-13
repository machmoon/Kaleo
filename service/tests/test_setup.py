"""The Setup Assistant's service (``service/setup.py``).

Two halves. The pure handlers are driven directly, with every outbound seam
replaced by something that either records or raises -- in demo mode the
raising ones prove that nothing leaves the process. The socket tests run
the real ``Handler`` on ``make_server(port=0)`` and pin the wire: the bearer
gate, the one public route, the HTML headers, and the ``Host`` check.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from billing.transport import HttpResponse
from service import billing_routes, deliver, envfiles, integrations, setup
from service.app import Handler, make_server
from service.cache import MemoryFactStore
from service.tests.test_app import scripted, url

GUID = "11111111-2222-3333-4444-555555555555"
TENANT = "99999999-8888-7777-6666-555555555555"
SECRET = "sEcReT-value-that-must-never-echo"
CLIENT_ID = "1234-abcd.apps.googleusercontent.com"


class Raising:
    """A transport that fails the test if anything reaches it."""

    def __init__(self, name: str):
        self.name = name

    def __call__(self, *args, **kwargs):
        raise AssertionError(f"{self.name}: an outbound call was attempted")

    def post(self, *args, **kwargs):
        raise AssertionError(f"{self.name}: an outbound POST was attempted")

    def get(self, *args, **kwargs):
        raise AssertionError(f"{self.name}: an outbound GET was attempted")


@pytest.fixture
def sealed(monkeypatch):
    """Every seam raises: whatever passes here made no network call."""
    setup.microsoft_transport_factory = lambda: Raising("microsoft")
    billing_routes.verify_transport_factory = lambda: Raising("stripe")
    deliver.transport_factory = lambda: Raising("google")
    monkeypatch.setattr("googleapps.auth.run_auth_flow", Raising("google oauth"))
    yield
    deliver.transport_factory = None


@pytest.fixture
def demo(monkeypatch, sealed):
    monkeypatch.setenv("KALEO_SETUP_MODE", "demo")


@pytest.fixture
def clean_env(monkeypatch):
    for key in (
        *envfiles.FILES["google"],
        *envfiles.FILES["microsoft"],
        *envfiles.FILES["billing"],
        "SILKSCREEN_ACCESS_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GOOGLEAPPS_TOKEN_PATH", str(envfiles.kaleo_home() / "gt.json"))


class EntraFake:
    """The token endpoint, answering from a table. Records every call."""

    def __init__(self, status=200, payload=None):
        self.status = status
        self.payload = payload or {"access_token": "TOKEN-VALUE", "expires_in": 3599}
        self.calls: list[tuple[str, bytes]] = []

    def post(self, url, headers, body):
        self.calls.append((url, body))
        return self.status, json.dumps(self.payload).encode()

    def get(self, url, headers):  # pragma: no cover - never used here
        raise AssertionError("GET")


def files_under(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


# ---------------------------------------------------------------- shape


def test_report_has_the_frozen_shape(clean_env, sealed):
    body = setup.setup_report()
    assert body["schema_version"] == 1
    assert body["mode"] == "live" and body["demo"] is False
    assert body["home"] == str(envfiles.kaleo_home())
    assert body["demo_home"] == str(envfiles.demo_dir())
    assert set(body) >= {"engine", "google", "microsoft", "stripe"}
    assert body["engine"] == {
        "state": "ready",
        "detail": "this engine answered /setup",
        "connect": {"kind": "none"},
        "demo": False,
    }
    g = body["google"]
    assert set(g) >= {
        "state",
        "detail",
        "demo",
        "oauth_client",
        "signed_in",
        "token",
        "job",
        "connect",
    }
    assert g["connect"] == {
        "kind": "oauth_browser",
        "start": "POST /setup/google/connect",
        "poll": "GET /setup/google",
        "credentials": "POST /setup/google/credentials",
        "disconnect": "POST /setup/google/disconnect",
    }
    assert g["job"]["state"] == "idle"
    m = body["microsoft"]
    assert set(m) >= {
        "state",
        "detail",
        "demo",
        "fields",
        "verified_at",
        "meaning",
        "connect",
    }
    assert [f["key"] for f in m["fields"]] == [
        "TEAMS_APP_ID",
        "TEAMS_APP_SECRET",
        "TEAMS_TENANT_ID",
    ]
    assert m["meaning"] == setup.MICROSOFT_MEANING
    assert m["unverified"] is True
    assert m["connect"]["kind"] == "credentials"
    s = body["stripe"]
    assert set(s) >= {
        "state",
        "detail",
        "demo",
        "ready",
        "key_mode",
        "steps",
        "connect",
    }
    assert s["connect"]["status"] == "GET /billing/config"
    assert s["connect"]["oauth"] == "POST /billing/connect"
    assert {step["key"] for step in s["steps"]} == {
        "STRIPE_API_KEY",
        "STRIPE_WEBHOOK_SECRET",
        "STRIPE_PRICE_ID",
    }
    for ident in ("google", "microsoft", "stripe"):
        assert body[ident]["state"] in (
            "ready",
            "partial",
            "unconfigured",
            "unavailable",
        )
        assert body[ident]["demo"] is False


def test_every_body_carries_mode_and_demo_live_never_demo(clean_env, sealed):
    status, body, _ = setup.handle_get("/setup", {})
    assert body["mode"] == "live" and body["demo"] is False
    for route in ("/setup/google", "/setup/microsoft", "/setup/stripe", "/setup/nope"):
        _, body, _ = setup.handle_get(route, {})
        assert body["mode"] == "live" and body["demo"] is False, route
    for route in (
        "/setup/google/disconnect",
        "/setup/microsoft/disconnect",
        "/setup/google/credentials",
        "/setup/microsoft/credentials",
        "/setup/stripe/credentials",
        "/setup/nope",
    ):
        _, body, _ = setup.handle_post(route, {}, base_url="http://127.0.0.1:1")
        assert body["mode"] == "live" and body["demo"] is False, route


def test_every_body_in_demo_says_demo_true(demo, clean_env):
    _, body, _ = setup.handle_get("/setup", {})
    assert body["mode"] == "demo" and body["demo"] is True
    assert body["banner"] == setup.DEMO_BANNER
    for ident in ("google", "microsoft", "stripe"):
        assert body[ident]["demo"] is True
    assert body["engine"]["demo"] is False  # the engine is real either way
    for route in ("/setup/google", "/setup/microsoft", "/setup/stripe"):
        _, body, _ = setup.handle_get(route, {})
        assert body["demo"] is True and body["mode"] == "demo"


def test_a_raising_probe_is_unavailable_not_a_500(clean_env, sealed, monkeypatch):
    def boom():
        raise RuntimeError("stripe exploded with rk_test_SHOULDNOTSHOW")

    monkeypatch.setattr(setup, "stripe_status", boom)
    body = setup.setup_report()
    assert body["stripe"]["state"] == "unavailable"
    assert body["google"]["state"] in ("ready", "partial", "unconfigured")
    assert "SHOULDNOTSHOW" not in json.dumps(body)


# ---------------------------------------------------------------- google


def test_google_state_agrees_with_integrations(clean_env, sealed, monkeypatch):
    for env in (
        {},
        {"GOOGLEAPPS_CLIENT_ID": CLIENT_ID, "GOOGLEAPPS_CLIENT_SECRET": "s"},
    ):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        entry = next(
            e
            for e in integrations.integrations_report()["integrations"]
            if e["id"] == "google"
        )
        g = setup.google_status()
        assert g["state"] == entry["state"]
        assert g["detail"] == entry["detail"]
        assert g["state"] in ("partial", "unconfigured")  # no token: never ready


def test_google_credentials_are_shape_checked_and_proven_at_sign_in(clean_env, sealed):
    status, body = setup.google_credentials({"client_id": "nope", "client_secret": "s"})
    assert status == 400 and ".apps.googleusercontent.com" in body["error"]
    assert not envfiles.env_path("google").exists()

    status, body = setup.google_credentials(
        {"client_id": CLIENT_ID, "client_secret": ""}
    )
    assert status == 400

    status, body = setup.google_credentials(
        {"client_id": CLIENT_ID, "client_secret": SECRET}
    )
    assert status == 200, body
    assert body["saved"] is True and body["verified"] is False
    assert body["active"] is True and body["active_source"] == "file"
    assert "proven at sign-in" in body["note"]
    assert os.environ["GOOGLEAPPS_CLIENT_SECRET"] == SECRET
    assert body["report"]["oauth_client"] is True
    assert body["report"]["state"] == "partial"
    assert SECRET not in json.dumps(body)


def test_google_save_reports_when_the_environment_still_wins(
    clean_env, sealed, monkeypatch
):
    monkeypatch.setenv("GOOGLEAPPS_CLIENT_SECRET", "from-the-shell")
    status, body = setup.google_credentials(
        {"client_id": CLIENT_ID, "client_secret": SECRET}
    )
    assert status == 200
    assert body["active"] is False and body["active_source"] == "environment"
    assert billing_routes.ENV_WINS_NOTE in body["note"]
    assert os.environ["GOOGLEAPPS_CLIENT_SECRET"] == "from-the-shell"


def test_google_disconnect_only_unsets_what_it_wrote(clean_env, sealed, monkeypatch):
    setup.google_credentials({"client_id": CLIENT_ID, "client_secret": SECRET})
    # Somebody exported a different client id in the shell meanwhile.
    monkeypatch.setenv("GOOGLEAPPS_CLIENT_ID", "other.apps.googleusercontent.com")
    status, body = setup.google_disconnect()
    assert status == 200
    assert body["disconnected"] is True
    assert "google.env" in body["removed"]
    assert body["still_configured_from_environment"] == ["GOOGLEAPPS_CLIENT_ID"]
    assert os.environ["GOOGLEAPPS_CLIENT_ID"] == "other.apps.googleusercontent.com"
    assert "GOOGLEAPPS_CLIENT_SECRET" not in os.environ
    assert body["note"] == deliver.SIGN_OUT_NOTE
    assert "not revoked" in body["note"]


def test_google_connect_live_needs_an_oauth_client(clean_env, sealed):
    status, body = setup.google_connect("http://127.0.0.1:1")
    assert status == 400
    assert "CLIENT" in body["error"].upper()


def test_google_connect_live_returns_the_url_and_a_waiting_job(clean_env, monkeypatch):
    from googleapps.tests.fakes import save_token, valid_token

    monkeypatch.setenv("GOOGLEAPPS_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("GOOGLEAPPS_CLIENT_SECRET", SECRET)
    release = threading.Event()

    def fake_flow(config, _transport, **kwargs):
        kwargs["on_url"]("https://accounts.google.com/o/oauth2/v2/auth?client=test")
        release.wait(5)
        save_token(config.token_path, valid_token())
        return config.token_path

    monkeypatch.setattr("googleapps.auth.run_auth_flow", fake_flow)
    status, body = setup.google_connect("http://127.0.0.1:1")
    assert status == 202, body
    assert body["auth_url"].startswith("https://accounts.google.com/")
    assert body["job"]["state"] == "waiting"
    assert setup.google_status()["job"]["state"] == "waiting"
    # A second connect while one waits is a 409, not a second browser tab.
    assert setup.google_connect("http://127.0.0.1:1")[0] == 409
    release.set()
    deliver.finish_auth()
    g = setup.google_status()
    assert g["job"]["state"] == "connected"
    assert g["signed_in"] is True and g["token"] == "valid"


# ---------------------------------------------------------------- google demo


def test_demo_google_connect_points_at_this_server_and_flips_on_allow(demo, clean_env):
    home = envfiles.kaleo_home()
    status, body = setup.google_connect("http://127.0.0.1:8099")
    assert status == 202
    assert body["demo"] is True if "demo" in body else True
    parsed = urllib.parse.urlsplit(body["auth_url"])
    assert (parsed.scheme, parsed.netloc, parsed.path) == (
        "http",
        "127.0.0.1:8099",
        setup.CONSENT_ROUTE,
    )
    query = dict(urllib.parse.parse_qsl(parsed.query))
    assert query["provider"] == "google" and len(query["state"]) >= 40
    assert setup.google_status()["job"]["state"] == "waiting"
    assert setup.google_status()["state"] == "partial"

    status, page = setup.demo_consent_page(query)
    assert status == 200 and "Allow" in page and "No real Google account" in page

    status, page = setup.demo_consent_decide({**query, "decision": "allow"})
    assert status == 200 and "close this tab" in page
    g = setup.google_status()
    assert g["state"] == "ready" and g["signed_in"] is True and g["demo"] is True
    assert g["detail"] == setup.GOOGLE_DEMO_DETAIL
    assert g["job"]["state"] == "connected"

    # Written only under demo/, marked, and never exported.
    assert files_under(home) == ["demo/google.env"]
    text = (home / "demo" / "google.env").read_text()
    assert text.startswith(envfiles.DEMO_HEADER) and "KALEO_DEMO=1" in text
    assert not any(k in os.environ for k in envfiles.FILES["google"])
    # /integrations keeps telling the truth.
    entry = next(
        e
        for e in integrations.integrations_report()["integrations"]
        if e["id"] == "google"
    )
    assert entry["state"] == "unconfigured"


def test_demo_consent_nonce_is_single_use_and_expires(demo, clean_env):
    _, body = setup.google_connect("http://127.0.0.1:1")
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(body["auth_url"]).query))
    assert (
        setup.demo_consent_page({**query, "state": query["state"][:-1] + "x"})[0] == 404
    )
    assert setup.demo_consent_page({"provider": "google", "state": ""})[0] == 404
    assert setup.demo_consent_page({**query, "provider": "microsoft"})[0] == 404
    assert setup.demo_consent_decide({**query, "decision": "sideways"})[0] == 400
    assert setup.demo_consent_decide({**query, "decision": "deny"})[0] == 200
    assert setup.google_status()["job"] == {
        "state": "failed",
        "error": "you declined the demo sign-in",
    }
    # Spent: neither the page nor a second decision works.
    assert setup.demo_consent_page(query)[0] == 404
    assert setup.demo_consent_decide({**query, "decision": "allow"})[0] == 404
    assert setup.google_status()["signed_in"] is False

    now = [1_000_000.0]
    setup.clock = lambda: now[0]
    _, body = setup.google_connect("http://127.0.0.1:1")
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(body["auth_url"]).query))
    now[0] += setup.NONCE_TTL_S + 1
    assert setup.demo_consent_page(query)[0] == 404
    assert setup.google_status()["job"]["state"] == "failed"
    assert "expired" in setup.google_status()["job"]["error"]


def test_demo_consent_is_404_in_live_mode(clean_env, sealed):
    assert setup.is_public_demo_route(setup.CONSENT_ROUTE) is False
    status, body, ctype = setup.handle_get(
        setup.CONSENT_ROUTE, {"provider": "google", "state": "x"}, host="127.0.0.1"
    )
    assert status == 404 and ctype == "application/json"
    status, body, ctype = setup.handle_post(
        setup.CONSENT_ROUTE,
        {},
        base_url="",
        form={"decision": "allow"},
        host="127.0.0.1",
    )
    assert status == 404 and ctype == "application/json"


def test_public_exemption_is_exact_not_a_prefix(demo):
    assert setup.is_public_demo_route("/setup/demo/consent") is True
    assert setup.is_public_demo_route("/setup/demo/consent?provider=google") is True
    assert setup.is_public_demo_route("/setup/demo/consent/") is False
    assert setup.is_public_demo_route("/setup/demo/consentx") is False
    assert setup.is_public_demo_route("/setup/demo") is False
    assert setup.is_public_demo_route("/setup") is False


def test_demo_google_disconnect_forgets_the_demo_file(demo, clean_env):
    _, body = setup.google_connect("http://127.0.0.1:1")
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(body["auth_url"]).query))
    setup.demo_consent_decide({**query, "decision": "allow"})
    status, body = setup.google_disconnect()
    assert status == 200 and body["removed"] == ["google.env"]
    assert setup.google_status()["state"] == "unconfigured"
    assert files_under(envfiles.kaleo_home()) == []


def test_demo_google_credentials_store_no_secret(demo, clean_env):
    status, body = setup.google_credentials(
        {"client_id": CLIENT_ID, "client_secret": SECRET}
    )
    assert status == 200 and body["saved"] is True and body["verified"] is False
    text = (envfiles.demo_dir() / "google.env").read_text()
    assert SECRET not in text and CLIENT_ID not in text
    assert setup.google_status()["state"] == "partial"
    assert SECRET not in json.dumps(body)


# ---------------------------------------------------------------- microsoft


def test_microsoft_live_verifies_then_persists(clean_env):
    entra = EntraFake()
    setup.microsoft_transport_factory = lambda: entra
    payload = {"app_id": GUID, "app_secret": SECRET, "tenant_id": TENANT}
    status, body = setup.microsoft_credentials({**payload, "verify_only": True})
    assert status == 200 and body["saved"] is False
    assert body["verdict"]["ok"] is True and body["verdict"]["expires_in"] == 3599
    assert not envfiles.env_path("microsoft").exists()

    status, body = setup.microsoft_credentials(payload)
    assert status == 200, body
    assert body["saved"] is True and body["label"] == "Token issued"
    assert body["meaning"] == setup.MICROSOFT_MEANING
    assert body["active"] is True and body["active_source"] == "file"
    assert os.environ["TEAMS_APP_SECRET"] == SECRET
    assert (
        envfiles.load_env(envfiles.env_path("microsoft"))["TEAMS_TENANT_ID"] == TENANT
    )
    m = body["report"]
    assert m["state"] == "ready" and m["verified_at"] and m["label"] == "Token issued"
    assert m["fields"][1] == {
        "key": "TEAMS_APP_SECRET",
        "set": True,
        "shown": f"<set, {len(SECRET)} chars>",
    }
    assert len(entra.calls) == 2
    assert (
        entra.calls[0][0]
        == f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
    )
    blob = json.dumps(body)
    assert SECRET not in blob and "TOKEN-VALUE" not in blob


def test_microsoft_refusal_is_not_persisted_and_carries_no_free_text(clean_env):
    description = (
        f"AADSTS7000215: Invalid client secret provided for {GUID}. "
        f"Trace ID: abc. secret starts {SECRET[:6]}"
    )
    entra = EntraFake(
        401, {"error": "invalid_client", "error_description": description}
    )
    setup.microsoft_transport_factory = lambda: entra
    status, body = setup.microsoft_credentials(
        {"app_id": GUID, "app_secret": SECRET, "tenant_id": TENANT}
    )
    assert status == 400
    assert body["saved"] is False
    assert body["verdict"] == {
        "ok": False,
        "status": 401,
        "code": "AADSTS7000215",
        "meaning": "the secret is wrong",
        "reason": "the secret is wrong",
        "expires_in": None,
    }
    assert body["meaning"] == setup.MICROSOFT_MEANING
    blob = json.dumps(body)
    assert SECRET[:6] not in blob and "Trace ID" not in blob and GUID not in blob
    assert not envfiles.env_path("microsoft").exists()
    assert "TEAMS_APP_SECRET" not in os.environ
    assert setup.microsoft_status()["state"] == "unconfigured"


@pytest.mark.parametrize(
    ("code", "meaning"),
    [
        ("AADSTS700016", "that app id is not in this tenant"),
        ("AADSTS90002", "tenant unknown"),
        ("AADSTS50034", "Entra refused; code 50034"),
    ],
)
def test_microsoft_codes_map_to_fixed_sentences(clean_env, code, meaning):
    entra = EntraFake(400, {"error": "x", "error_description": f"{code}: whatever"})
    setup.microsoft_transport_factory = lambda: entra
    _, body = setup.microsoft_credentials(
        {"app_id": GUID, "app_secret": SECRET, "tenant_id": TENANT}
    )
    assert body["verdict"]["code"] == code
    assert body["verdict"]["meaning"] == meaning


def test_microsoft_missing_fields_are_400_without_a_call(clean_env, sealed):
    status, body = setup.microsoft_credentials({"app_id": GUID})
    assert status == 400 and body["verdict"]["ok"] is False


def test_microsoft_disconnect_only_unsets_what_it_wrote(clean_env, monkeypatch):
    setup.microsoft_transport_factory = lambda: EntraFake()
    setup.microsoft_credentials(
        {"app_id": GUID, "app_secret": SECRET, "tenant_id": TENANT}
    )
    monkeypatch.setenv("TEAMS_TENANT_ID", "another-tenant")
    status, body = setup.microsoft_disconnect()
    assert status == 200
    assert body["removed"] == ["microsoft.env"]
    assert body["still_configured_from_environment"] == ["TEAMS_TENANT_ID"]
    assert "TEAMS_APP_SECRET" not in os.environ and "TEAMS_APP_ID" not in os.environ
    m = setup.microsoft_status()
    assert m["state"] == "partial" and m["verified_at"] is None


def test_demo_microsoft_accepts_guids_by_shape_and_never_contacts_entra(
    demo, clean_env
):
    status, body = setup.microsoft_credentials(
        {"app_id": "not-a-guid", "app_secret": SECRET, "tenant_id": TENANT}
    )
    assert status == 400 and "GUID" in body["verdict"]["meaning"]
    status, body = setup.microsoft_credentials(
        {"app_id": GUID, "app_secret": SECRET, "tenant_id": TENANT}
    )
    assert status == 200, body
    assert body["saved"] is True and body["label"] == "Demo"
    assert body["verdict"]["meaning"] == setup.MICROSOFT_DEMO_MEANING
    assert body["meaning"] == setup.MICROSOFT_DEMO_MEANING
    m = setup.microsoft_status()
    assert m["state"] == "ready" and m["demo"] is True and m["unverified"] is True
    assert files_under(envfiles.kaleo_home()) == ["demo/microsoft.env"]
    text = (envfiles.demo_dir() / "microsoft.env").read_text()
    assert SECRET not in text and GUID not in text
    assert not any(k in os.environ for k in envfiles.FILES["microsoft"])
    assert SECRET not in json.dumps(body)


# ---------------------------------------------------------------- stripe


def stripe_ok(request):
    assert request.headers["Authorization"].startswith("Bearer ")
    return HttpResponse(
        200,
        b'{"livemode": false, "active": true, "currency": "usd", "unit_amount": 500}',
    )


def test_stripe_live_delegates_to_billing_routes(clean_env):
    calls = []

    def transport(request):
        calls.append(request.url)
        return stripe_ok(request)

    billing_routes.verify_transport_factory = lambda: transport
    status, body = setup.stripe_credentials(
        {
            "STRIPE_API_KEY": "rk_test_51abcdefghijklmnop",
            "STRIPE_WEBHOOK_SECRET": "whsec_x",
            "STRIPE_PRICE_ID": "price_1",
        }
    )
    assert status == 200, body
    assert body["saved"] is True and body["check"]["ok"] is True
    assert body["note"].startswith(setup.STRIPE_LIVE_NOTE)
    assert body["active"] is True and body["active_source"] == "file"
    assert calls == ["https://api.stripe.com/v1/prices/price_1"]
    assert os.environ["STRIPE_API_KEY"] == "rk_test_51abcdefghijklmnop"
    assert (
        envfiles.load_env(envfiles.env_path("billing"))["STRIPE_PRICE_ID"] == "price_1"
    )
    s = body["report"]
    assert s["state"] == "ready" and s["ready"] is True and s["key_mode"] == "test"
    assert "rk_test_51abcdefghijklmnop" not in json.dumps(body)


def test_stripe_demo_accepts_only_the_literal_demo_key(demo, clean_env):
    home = envfiles.kaleo_home()
    for key, fragment in (
        ("rk_live_51abcdefghijklmnopq", "live keys are never accepted"),
        ("sk_live_x", "live keys are never accepted"),
        ("rk_test_51abcdefghijklmnopq", "looks like a real test key"),
        ("rk_test_short", "accepts only the demo key"),
        ("", "accepts only the demo key"),
    ):
        status, body = setup.stripe_credentials({"STRIPE_API_KEY": key})
        assert status == 400, key
        assert fragment in body["error"], key
        assert files_under(home) == [], key
        assert "STRIPE_API_KEY" not in os.environ

    status, body = setup.stripe_credentials({"STRIPE_API_KEY": setup.DEMO_STRIPE_KEY})
    assert status == 200, body
    assert body["saved"] is True and body["key_mode"] == "demo"
    assert body["check"]["ok"] is True
    assert files_under(home) == ["demo/billing.env"]
    text = (home / "demo" / "billing.env").read_text()
    assert "STRIPE_KEY_MODE=demo" in text and "KALEO_DEMO=1" in text
    assert setup.DEMO_STRIPE_KEY not in text
    assert "STRIPE_API_KEY" not in os.environ
    s = setup.stripe_status()
    assert s["state"] == "ready" and s["ready"] is True and s["demo"] is True
    assert s["key_mode"] == "demo" and s["connect"]["oauth"] is None
    assert s["demo_key"] == setup.DEMO_STRIPE_KEY
    # /billing/config (what /integrations reads) still sees nothing.
    _, report = billing_routes.handle_config_get()
    assert report["ready"] is False


def test_stripe_demo_uses_the_demo_transport_not_the_live_seam(demo, clean_env):
    seen = []

    def demo_transport(request):
        seen.append(request.headers["Authorization"])
        return HttpResponse(200, b"{}")

    setup.demo_stripe_transport_factory = lambda: demo_transport
    status, _ = setup.stripe_credentials({"STRIPE_API_KEY": setup.DEMO_STRIPE_KEY})
    assert status == 200
    assert seen == [f"Bearer {setup.DEMO_STRIPE_KEY}"]


# ---------------------------------------------------------------- wire


@pytest.fixture
def server(clean_env, sealed, monkeypatch):
    monkeypatch.setenv("SILKSCREEN_STEPS_DIR", str(envfiles.kaleo_home() / "steps"))
    Handler.model_factory = staticmethod(scripted)
    Handler.store = MemoryFactStore()
    srv = make_server(port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    Handler.store = None


def call(srv, path, *, method="GET", payload=None, form=None, headers=None):
    hdrs = dict(headers or {})
    data = None
    if payload is not None:
        hdrs["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()
    if form is not None:
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url(srv, path), data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def test_every_route_answers_on_the_wire(server):
    status, headers, body = call(server, "/setup")
    assert status == 200 and headers["Cache-Control"] == "no-store"
    assert json.loads(body)["schema_version"] == 1
    for route in ("/setup/google", "/setup/microsoft", "/setup/stripe", "/setup/voice"):
        status, headers, body = call(server, route)
        assert status == 200 and headers["Cache-Control"] == "no-store", route
        assert json.loads(body)["mode"] == "live"
    for route, payload, expected in (
        ("/setup/google/connect", {}, 400),
        ("/setup/google/disconnect", {}, 200),
        ("/setup/google/credentials", {"client_id": "x", "client_secret": "y"}, 400),
        ("/setup/microsoft/credentials", {}, 400),
        ("/setup/microsoft/disconnect", {}, 200),
        ("/setup/stripe/credentials", {}, 200),
        ("/setup/nothing", {}, 404),
    ):
        status, headers, body = call(server, route, method="POST", payload=payload)
        assert status == expected, (route, body)
        assert headers["Cache-Control"] == "no-store"
        assert json.loads(body)["mode"] == "live"
    # Live: the consent route does not exist, and needs no special casing.
    assert call(server, "/setup/demo/consent?provider=google&state=x")[0] == 404
    assert call(server, "/setup/demo/consent", method="POST", form={"a": "b"})[0] == 404


def test_bearer_is_required_on_every_setup_route_but_the_demo_consent(
    server, monkeypatch
):
    monkeypatch.setenv("SILKSCREEN_ACCESS_TOKEN", "tok-1")
    for route in (
        "/setup",
        "/setup/google",
        "/setup/microsoft",
        "/setup/stripe",
        "/setup/voice",
    ):
        assert call(server, route)[0] == 401, route
        assert call(server, route, headers={"Authorization": "Bearer tok-1"})[0] == 200
    for route in (
        "/setup/google/connect",
        "/setup/google/credentials",
        "/setup/microsoft/credentials",
        "/setup/stripe/credentials",
        "/setup/google/disconnect",
        "/setup/microsoft/disconnect",
    ):
        status, _, _ = call(server, route, method="POST", payload={"client_id": "x"})
        assert status == 401, route
    status, _, body = call(
        server,
        "/setup/google/credentials",
        method="POST",
        payload={"client_id": CLIENT_ID, "client_secret": SECRET},
        headers={"Authorization": "Bearer wrong"},
    )
    assert status == 401
    assert not envfiles.env_path("google").exists()

    monkeypatch.setenv("KALEO_SETUP_MODE", "demo")
    status, _, body = call(
        server,
        "/setup/google/connect",
        method="POST",
        payload={},
        headers={"Authorization": "Bearer tok-1"},
    )
    assert status == 202, body
    auth_url = json.loads(body)["auth_url"]
    assert auth_url.startswith(
        f"http://127.0.0.1:{server.server_port}/setup/demo/consent?"
    )
    path = auth_url.split(f"127.0.0.1:{server.server_port}", 1)[1]
    # The consent page is the one public route: a browser tab has no bearer.
    status, headers, page = call(server, path)
    assert status == 200, page
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["Content-Security-Policy"] == (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'"
    )
    assert b"Allow" in page
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(path).query))
    status, headers, page = call(
        server,
        "/setup/demo/consent",
        method="POST",
        form={**query, "decision": "allow"},
    )
    assert status == 200 and b"close this tab" in page
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    status, _, body = call(
        server, "/setup/google", headers={"Authorization": "Bearer tok-1"}
    )
    assert json.loads(body)["state"] == "ready"
    assert json.loads(body)["demo"] is True
    # And the exemption is exact: a sibling path under /setup/demo is gated.
    assert call(server, "/setup/demo/consent/extra")[0] == 401


def test_consent_with_a_foreign_host_is_403(server, monkeypatch):
    monkeypatch.setenv("KALEO_SETUP_MODE", "demo")
    _, _, body = call(server, "/setup/google/connect", method="POST", payload={})
    path = json.loads(body)["auth_url"].split(f"127.0.0.1:{server.server_port}", 1)[1]
    status, _, _ = call(server, path, headers={"Host": "attacker.example"})
    assert status == 403
    status, _, _ = call(
        server,
        "/setup/demo/consent",
        method="POST",
        form={"provider": "google", "state": "x", "decision": "allow"},
        headers={"Host": "attacker.example:8081"},
    )
    assert status == 403
    assert setup.google_status()["state"] == "partial"  # still waiting, not flipped
    # The consent link itself is built from a foreign Host only if loopback.
    status, _, body = call(
        server,
        "/setup/google/disconnect",
        method="POST",
        payload={},
    )
    _, _, body = call(
        server,
        "/setup/google/connect",
        method="POST",
        payload={},
        headers={"Host": "attacker.example"},
    )
    assert json.loads(body)["auth_url"].startswith(
        f"http://127.0.0.1:{server.server_port}/"
    )


def test_consent_post_requires_a_form_and_is_bounded(server, monkeypatch):
    monkeypatch.setenv("KALEO_SETUP_MODE", "demo")
    status, _, _ = call(server, "/setup/demo/consent", method="POST", payload={"a": 1})
    assert status == 415
    req = urllib.request.Request(
        url(server, "/setup/demo/consent"),
        data=b"a=" + b"b" * 5000,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(req, timeout=30)
    assert caught.value.code == 413


def test_host_is_local_accepts_loopback_forms_only():
    assert setup.host_is_local("127.0.0.1:8081")
    assert setup.host_is_local("localhost")
    assert setup.host_is_local("[::1]:8081")
    assert setup.host_is_local("LOCALHOST:1")
    assert not setup.host_is_local("attacker.example")
    assert not setup.host_is_local("127.0.0.1.attacker.example")
    assert not setup.host_is_local("")
    assert not setup.host_is_local(None)
    assert setup.host_is_local("engine.local:8081", "engine.local")
    assert not setup.host_is_local("0.0.0.0:8081", "0.0.0.0")


# ---------------------------------------------------------------- voice


def test_voice_is_unconfigured_and_names_the_fix_when_no_weights(monkeypatch):
    """A fresh machine must say what is missing AND the command that fixes it.

    This is the provisioning cliff made visible. `service/tts.py` refuses to
    download 340 MB of weights on demand, which is right; the cost of that
    refusal used to be paid silently, because the client fell through to the
    webview's `speechSynthesis` and the macOS Compact voice quietly became
    what Ada sounded like. The client no longer falls through, so an
    unprovisioned engine now means silence -- and silence has to be
    explainable from the report or it just reads as broken.
    """
    monkeypatch.setenv("KALEO_KOKORO_MODEL", "/nonexistent/model.onnx")
    monkeypatch.setenv("KALEO_KOKORO_VOICES", "/nonexistent/voices.bin")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)

    report = setup.voice_status()

    # `unconfigured`, not `unavailable`: the gap is a download away, and the
    # two words send a person to different places.
    assert report["state"] == "unconfigured"
    assert report["selected"] is None
    # It must say Ada goes QUIET rather than implying she falls back.
    assert "silent" in report["detail"]
    # And it must name the one command, not merely describe the problem.
    assert any("install_voice.sh" in hint for hint in report["hints"])
    # The per-engine detail survives, so "why not ElevenLabs either" is
    # answerable from the same body.
    assert {e["name"] for e in report["engines"]} >= {"kokoro", "elevenlabs"}


def test_voice_state_agrees_with_speak_report_rather_than_re_deriving_it():
    """One answer to "is there a voice", for `google_status`'s reason.

    Two separately computed answers to the same question eventually disagree,
    and then neither can be trusted. `/setup` must therefore be a view of
    `speak_report`, never a second opinion about it.
    """
    from service import tts

    selected = tts.speak_report(tts.build_engines())["selected"]
    report = setup.voice_status()

    assert report["selected"] == selected
    assert (report["state"] == "ready") == (selected is not None)
