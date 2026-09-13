"""The application bearer gate (``SILKSCREEN_ACCESS_TOKEN``).

``scripts/deploy.sh`` deploys the service ``--allow-unauthenticated`` on
purpose -- the client is a desktop app whose users have no Google identity for
Cloud Run IAM to check -- and says in as many words that the real gate is this
token. These tests are what makes that sentence true rather than a comment: for
a long time the deploy script injected the secret and nothing read it, so every
"gated" deploy was an open, quota-spending endpoint.

Two halves, both of which have to hold:

* with no token in the environment there is no gate at all, because that is
  how the service is run locally and how the desktop app and the SPA reach it
  over loopback; and
* with one set, everything that spends money needs ``Authorization: Bearer``,
  while the probes Cloud Run calls and the static bundle a browser navigates to
  stay open, since neither can carry a header of ours.
"""

import json
import socket
import threading
import urllib.error
import urllib.request

import pytest

from service.app import Handler, make_server
from service.cache import MemoryFactStore
from service.tests.test_app import scripted, url

TOKEN = "s3cret-token-value"


@pytest.fixture
def server(offline_pdf_fetch, tmp_path):
    """The real surface, plus a two-file bundle so the static arm is testable."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>app</title>", "utf-8")
    (dist / "assets" / "app-abc123.js").write_text("export const ok = true;\n", "utf-8")

    previous_root = Handler.web_root
    Handler.web_root = dist
    Handler.model_factory = staticmethod(scripted)
    Handler.store = MemoryFactStore()
    srv = make_server(port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    Handler.store = None
    Handler.web_root = previous_root


@pytest.fixture
def gated(monkeypatch):
    """Turn the gate on. Read per request, so setting it here is enough."""
    monkeypatch.setenv("SILKSCREEN_ACCESS_TOKEN", TOKEN)


@pytest.fixture(autouse=True)
def _no_ambient_token(monkeypatch):
    """A token exported in the developer's shell must not decide these tests."""
    monkeypatch.delenv("SILKSCREEN_ACCESS_TOKEN", raising=False)


def request(srv, path, *, method="GET", token=None, header=None, payload=None):
    """One request, returning ``(status, body_bytes)`` for any status."""
    headers = {}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if header is not None:
        headers["Authorization"] = header
    elif token is not None:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        url(srv, path),
        data=None if payload is None else json.dumps(payload).encode(),
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


GENERATE = {"intent": "a 3.3V regulator", "time_limit_s": 5, "review": False}


def test_no_token_configured_leaves_the_service_open(server):
    """The local contract: nothing about running unauthenticated changes."""
    status, body = request(server, "/generate", method="POST", payload=GENERATE)
    assert status == 200, body
    assert json.loads(body)["kicad_pcb"].startswith("(kicad_pcb")

    for route in ("/healthz", "/readyz", "/models", "/config/status", "/"):
        assert request(server, route)[0] == 200, route


def test_the_configured_token_is_accepted(server, gated):
    status, body = request(
        server, "/generate", method="POST", token=TOKEN, payload=GENERATE
    )
    assert status == 200, body
    assert json.loads(body)["parts"]


def test_the_scheme_the_desktop_client_sends_is_the_scheme_accepted(server, gated):
    """``app/src/lib/silkscreen/client.ts`` sends exactly ``Bearer <token>``.

    Written as a literal header rather than through the helper so a change to
    the helper cannot quietly make this agree with itself instead of with the
    client. The scheme token is case-insensitive per RFC 7235, so ``bearer``
    is accepted too -- a proxy is allowed to have normalised it.
    """
    for header in (f"Bearer {TOKEN}", f"bearer {TOKEN}"):
        status, body = request(
            server, "/generate", method="POST", header=header, payload=GENERATE
        )
        assert status == 200, (header, body)


def test_a_wrong_token_is_401(server, gated):
    for wrong in (TOKEN + "x", TOKEN[:-1], TOKEN.upper(), "", "   "):
        status, body = request(
            server,
            "/generate",
            method="POST",
            header=f"Bearer {wrong}",
            payload=GENERATE,
        )
        assert status == 401, (wrong, body)
        assert "error" in json.loads(body)


def test_a_missing_or_wrong_scheme_header_is_401(server, gated):
    """No header at all, and the right token under the wrong scheme."""
    status, _ = request(server, "/generate", method="POST", payload=GENERATE)
    assert status == 401
    for header in (TOKEN, f"Basic {TOKEN}", f"Token {TOKEN}"):
        status, _ = request(
            server, "/generate", method="POST", header=header, payload=GENERATE
        )
        assert status == 401, header


def test_every_paid_post_route_is_gated(server, gated):
    """Not just /generate. Each of these reaches a model, a solver, or a run."""
    for route in (
        "/",
        "/generate",
        "/generate/stream",
        "/chat/stream",
        "/placement/repair",
        "/transcribe",
        "/desk/resolve",
        "/steps",
        "/billing/checkout",
    ):
        status, _ = request(server, route, method="POST", payload={})
        assert status == 401, route


def test_the_json_get_routes_are_gated(server, gated):
    """These describe the deployment or hand back somebody's run."""
    for route in ("/models", "/config/status", "/integrations", "/deliver/config"):
        assert request(server, route)[0] == 401, route
        assert request(server, route, token=TOKEN)[0] == 200, route


def test_the_probes_stay_open(server, gated):
    """Cloud Run's start-up and liveness checks send no header of ours.

    Gating them would fail every revision before it ever served a request,
    which is why they are answered before the gate rather than behind it.
    """
    for route in ("/healthz", "/readyz"):
        status, body = request(server, route)
        assert status == 200, route
        assert json.loads(body)["ok"] is True


def test_the_static_bundle_stays_open(server, gated):
    """A browser cannot put a bearer header on a document navigation.

    So the bundle is served to anyone: it is public build output with no secret
    in it, and every route it goes on to call is gated. Gating it would serve a
    401 body where the app should be, and change nothing about who can spend
    the owner's quota.
    """
    status, body = request(server, "/")
    assert status == 200 and b"<!doctype html>" in body
    status, body = request(server, "/index.html")
    assert status == 200 and b"<!doctype html>" in body
    status, body = request(server, "/assets/app-abc123.js")
    assert status == 200 and b"export const ok" in body


def test_a_missing_bundle_file_is_still_gated(server, gated):
    """The static exemption is for files that exist, not for any GET.

    Otherwise a 404 would be an unauthenticated probe of the route table.
    """
    assert request(server, "/assets/not-built.js")[0] == 401
    assert request(server, "/steps/deadbeef")[0] == 401


def test_the_stripe_webhook_is_exempt_and_verifies_itself(server, gated):
    """Stripe cannot send our token; it signs its own raw body instead.

    That HMAC is a stronger check than a shared bearer, and gating the route
    would silently drop every payment event. The exemption is safe only
    because the signature check is unconditional, which ``handle_webhook``
    owns and ``test_billing_routes.py`` covers. What matters here is only that
    the gate is not what answers: unsigned, this request reaches the billing
    module (which, with no signing secret configured in the test environment,
    refuses it on its own terms) rather than stopping at a 401.
    """
    status, _ = request(
        server, "/billing/webhook", method="POST", payload={"type": "x"}
    )
    assert status != 401


def test_the_refusal_closes_the_connection(server, gated):
    """The body of a rejected request is never read, so the socket cannot be
    reused -- the unread bytes would be parsed as the next request line."""
    body = json.dumps(GENERATE).encode()
    raw = (
        b"POST /generate HTTP/1.1\r\n"
        + f"Host: 127.0.0.1:{server.server_port}\r\n".encode()
        + b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\n".encode()
        + b"\r\n"
        + body
    )
    with socket.create_connection(("127.0.0.1", server.server_port), 10) as sock:
        sock.sendall(raw)
        chunks = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
    response = b"".join(chunks)
    assert response.startswith(b"HTTP/1.0 401 ") or b" 401 " in response.split(b"\r\n")[
        0
    ]
    assert b"Connection: close" in response
