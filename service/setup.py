"""The first-launch Setup Assistant's service: ``GET /setup`` and friends.

The desktop wizard (``app/src/pages/welcome``) asks one question per screen --
connect Google, connect Microsoft, set up billing -- and this module is what
it asks. It owns no engine logic and reaches no account of its own: Google
sign-in is ``service.deliver``'s job, the Stripe check is
``service.billing_routes``', the Entra check is ``teamsbot.graph``'s, and
what each of those saved lives in ``service.envfiles``. What this module
adds is one shape for all three, the demo mode, and the honesty rules.

Routes (all behind the bearer gate, all ``Cache-Control: no-store``)::

    GET  /setup                          the whole report
    GET  /setup/google|microsoft|stripe  one provider, non-blocking
    GET  /setup/voice                    which voice Ada has, and the one
                                         command that provisions it if none
    POST /setup/google/connect           202 {auth_url, job}; the client opens it
    POST /setup/google/disconnect        delete the token here (not at Google)
    POST /setup/google/credentials       {client_id, client_secret}: shape only
    POST /setup/microsoft/credentials    {app_id, app_secret, tenant_id, verify_only?}
    POST /setup/microsoft/disconnect
    POST /setup/stripe/credentials       billing_routes' payload
    GET  /setup/demo/consent?provider=google&state=<nonce>    demo only, PUBLIC
    POST /setup/demo/consent             form: provider, state, decision

Every JSON body carries ``mode`` (``live`` or ``demo``) and every integration
object carries ``demo``; ``state`` uses the ``/integrations`` vocabulary
(``ready`` / ``partial`` / ``unconfigured`` / ``unavailable``), and in live
mode the Google state is copied from ``/integrations`` rather than derived
again, so the two routes cannot disagree.

**Demo mode is server-side and nothing else.** ``KALEO_SETUP_MODE=demo`` makes
the connect steps record pretend sign-ins under ``~/.kaleo/demo/`` -- files
that carry ``KALEO_DEMO=1``, are never exported into ``os.environ`` and are
never read at startup -- and makes **zero outbound calls**: the Entra
transport is never built, the Stripe check goes to an in-process transport
that recognises one literal key, and the Google consent page is served by
this process. ``/integrations`` keeps saying ``unconfigured`` in demo, which
is the truth, and ``/setup`` says ``ready, demo: true``, which is also the
truth. The client never simulates a connection itself.

The consent page is the one public route (``is_public_demo_route``): it is
opened by a browser that cannot send the bearer, so it is gated by a 256-bit
single-use nonce with a ten-minute life instead, only in demo mode, and
only for a request whose ``Host`` is loopback -- a page on
``attacker.example`` cannot make a browser's DNS-rebound request pass that.
"""

from __future__ import annotations

import hmac
import html
import os
import re
import secrets
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from . import billing_routes as _billing
from . import deliver as _deliver
from . import envfiles as _envfiles
from . import integrations as _integrations

__all__ = [
    "CONSENT_ROUTE",
    "DEMO_BANNER",
    "DEMO_STRIPE_KEY",
    "GOOGLE_DEMO_DETAIL",
    "MICROSOFT_DEMO_MEANING",
    "MICROSOFT_MEANING",
    "MODE_ENV",
    "NONCE_TTL_S",
    "SCHEMA_VERSION",
    "STRIPE_LIVE_NOTE",
    "DemoJob",
    "clock",
    "demo_consent_decide",
    "demo_consent_page",
    "demo_stripe_transport_factory",
    "engine_status",
    "google_connect",
    "google_credentials",
    "google_disconnect",
    "google_status",
    "handle_get",
    "handle_post",
    "host_is_local",
    "is_demo",
    "is_public_demo_route",
    "microsoft_credentials",
    "microsoft_disconnect",
    "microsoft_status",
    "microsoft_transport_factory",
    "mode",
    "reset_for_tests",
    "setup_report",
    "stripe_credentials",
    "stripe_status",
]

SCHEMA_VERSION = 1
MODE_ENV = "KALEO_SETUP_MODE"
CONSENT_ROUTE = "/setup/demo/consent"
#: The one key the demo Stripe step accepts. Anything else is refused before
#: any check runs, so no real key is ever written under ``demo/``.
DEMO_STRIPE_KEY = "rk_test_kaleo_demo"
NONCE_TTL_S = 600.0

MICROSOFT_MEANING = (
    "Entra issued an app token for this registration. That proves the ids "
    "and the secret; it does not prove Graph permissions are consented or "
    "that the Teams bot (`python -m teamsbot`) is running."
)
MICROSOFT_DEMO_MEANING = "Demo: ids accepted by shape. Entra was not contacted."
GOOGLE_DEMO_DETAIL = "Demo sign-in recorded. No Google account was contacted."
STRIPE_LIVE_NOTE = (
    "Key verified with Stripe. Webhook secret and price are not proven until "
    "a payment."
)
DEMO_BANNER = (
    "Demo mode. Nothing here reaches Google, Microsoft or Stripe; "
    "Integrations will still show them as unconfigured."
)
_GOOGLE_CLIENT_SUFFIX = ".apps.googleusercontent.com"
_GUIDISH = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
_LOOPBACK_ONLY = "the consent page answers loopback only"

#: Keys the demo files may carry. None of them is a credential.
_DEMO_KEYS = {
    "google": frozenset({"GOOGLE_OAUTH_CLIENT", "GOOGLE_SIGNED_IN"}),
    "microsoft": frozenset({"MICROSOFT_ACCEPTED", "KALEO_ACCEPTED_AT"}),
    "billing": frozenset({"STRIPE_KEY_MODE"}),
}

# ---------------------------------------------------------------- seams

#: Time, for the nonce TTL. Tests advance it.
clock: Callable[[], float] = time.time

#: ``None`` means ``teamsbot.graph.UrllibTransport`` -- the real thing. Never
#: called in demo mode; the demo tests install one that raises to prove it.
microsoft_transport_factory: Callable[[], Any] | None = None


def _demo_stripe_transport() -> Callable[[Any], Any]:
    """An in-process Stripe that knows exactly one key."""
    from billing.transport import HttpResponse

    def transport(request: Any) -> Any:
        presented = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if hmac.compare_digest(presented.encode(), DEMO_STRIPE_KEY.encode()):
            return HttpResponse(
                200, b'{"object": "list", "data": [], "livemode": false}'
            )
        return HttpResponse(
            401,
            b'{"error": {"type": "invalid_request_error", "code": "demo_key_only"}}',
        )

    return transport


demo_stripe_transport_factory: Callable[[], Any] = _demo_stripe_transport

# ---------------------------------------------------------------- mode


def mode() -> str:
    """``demo`` when ``KALEO_SETUP_MODE=demo`` is in the environment, else ``live``."""
    value = (os.environ.get(MODE_ENV) or "").strip().lower()
    return "demo" if value == "demo" else "live"


def is_demo() -> bool:
    return mode() == "demo"


def is_public_demo_route(path: str) -> bool:
    """Exactly the consent route, exactly in demo mode. Never a prefix."""
    return urllib.parse.urlsplit(path).path == CONSENT_ROUTE and is_demo()


def host_is_local(host: str | None, server_host: str | None = None) -> bool:
    """Is this ``Host`` header loopback (or the engine's own host)?

    The DNS-rebinding guard for the public consent route: a browser that
    was pointed at ``attacker.example`` and rebound to 127.0.0.1 still sends
    ``Host: attacker.example``.
    """
    if not host:
        return False
    name = host.strip().lower()
    if name.startswith("["):
        name = name[1 : name.index("]")] if "]" in name else name
    elif name.count(":") == 1:
        name = name.rsplit(":", 1)[0]
    if name in _LOOPBACK:
        return True
    own = (server_host or "").strip().lower()
    return bool(own) and own not in ("0.0.0.0", "::") and name == own


# ---------------------------------------------------------------- state

_lock = threading.Lock()
_demo_jobs: dict[str, DemoJob] = {}
_ms_verified_at: str | None = None


@dataclass
class DemoJob:
    """One pretend sign-in: a nonce the consent page must present back.

    256 bits from ``secrets``, compared with ``hmac.compare_digest``, spent
    on first use, dead after :data:`NONCE_TTL_S`.
    """

    provider: str
    nonce: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    created: float = field(default_factory=lambda: clock())
    state: str = "waiting"
    error: str | None = None
    used: bool = False

    def expired(self, now: float | None = None) -> bool:
        now = clock() if now is None else now
        return now - self.created > NONCE_TTL_S

    def matches(self, state: str | None) -> bool:
        if not state or self.used or self.expired():
            return False
        return hmac.compare_digest(self.nonce.encode(), str(state).encode())

    def status(self) -> dict[str, Any]:
        if self.state == "waiting" and self.expired():
            return {"state": "failed", "error": "the demo consent link expired"}
        return {"state": self.state, "error": self.error}


def reset_for_tests() -> None:
    global _ms_verified_at
    with _lock:
        _demo_jobs.clear()
        _ms_verified_at = None
    _deliver.cancel_auth()


# ---------------------------------------------------------------- helpers


def _mask(value: str) -> str:
    return f"<set, {len(value)} chars>" if value else ""


def _stamp(body: dict[str, Any]) -> dict[str, Any]:
    body.setdefault("mode", mode())
    body.setdefault("demo", is_demo())
    return body


def _now_iso() -> str:
    return datetime.fromtimestamp(clock(), tz=UTC).isoformat(timespec="seconds")


def _demo_values(name: str) -> tuple[dict[str, str], list[str]]:
    """The demo file for ``name``, plus hints for anything wrong with it."""
    path = _envfiles.env_path(name, demo=True)
    try:
        return _envfiles.load_env(path, demo=True), []
    except _envfiles.EnvFileError as exc:
        return {}, [str(exc)]
    except OSError as exc:
        return {}, [f"{path}: {exc.strerror or exc}"]


def _write_demo(name: str, values: Mapping[str, str]) -> None:
    _envfiles.save_env(
        _envfiles.env_path(name, demo=True),
        values,
        allow=_DEMO_KEYS[name],
        demo=True,
    )


def _live_values(name: str) -> tuple[dict[str, str], list[str]]:
    path = _envfiles.env_path(name)
    hints: list[str] = []
    try:
        values = _envfiles.load_env(path)
    except _envfiles.EnvFileError as exc:
        return {}, [str(exc)]
    except OSError as exc:
        return {}, [f"{path}: {exc.strerror or exc}"]
    hint = _envfiles.mode_hint(path)
    if hint:
        hints.append(hint)
    return values, hints


def _save_live(name: str, values: Mapping[str, str]) -> dict[str, Any]:
    """Save, apply (setdefault), and say whether the process is using it."""
    _envfiles.save_env(_envfiles.env_path(name), values, allow=_envfiles.FILES[name])
    _envfiles.apply_env(values, allow=_envfiles.FILES[name])
    active, source = _envfiles.activation(values)
    body: dict[str, Any] = {"active": active, "active_source": source}
    if not active:
        body["note"] = _billing.ENV_WINS_NOTE
    return body


def _disconnect_live(name: str) -> dict[str, Any]:
    """Remove the file; drop from ``os.environ`` only what the file put there."""
    path = _envfiles.env_path(name)
    values, _ = _live_values(name)
    removed: list[str] = []
    if _envfiles.remove_env(path):
        removed.append(path.name)
    for key, value in values.items():
        if key in _envfiles.FILES[name] and os.environ.get(key) == value:
            del os.environ[key]
    still = [key for key in sorted(_envfiles.FILES[name]) if os.environ.get(key)]
    return {
        "disconnected": True,
        "removed": removed,
        "still_configured_from_environment": still,
    }


def _disconnect_demo(name: str, provider: str) -> dict[str, Any]:
    path = _envfiles.env_path(name, demo=True)
    removed: list[str] = []
    if not path.is_symlink() and path.exists():
        _envfiles.remove_env(path)
        removed.append(path.name)
    with _lock:
        _demo_jobs.pop(provider, None)
    return {
        "disconnected": True,
        "removed": removed,
        "still_configured_from_environment": [],
    }


def _unavailable(ident: str, exc: BaseException) -> dict[str, Any]:
    """A probe raised; the report still answers for the other providers."""
    return {
        "state": "unavailable",
        "detail": f"{ident} could not be inspected",
        "hints": [f"checking {ident} raised {type(exc).__name__}"],
        "demo": is_demo(),
    }


# ---------------------------------------------------------------- engine


def engine_status() -> dict[str, Any]:
    """The request reached this process, which is the whole claim."""
    return {
        "state": "ready",
        "detail": "this engine answered /setup",
        "connect": {"kind": "none"},
        "demo": False,
    }


# ---------------------------------------------------------------- voice


def voice_status() -> dict[str, Any]:
    """Which voice Ada has, in the wizard's four-state vocabulary.

    This exists because of a provisioning cliff with teeth. Kokoro needs
    ~340 MB of weights that nothing downloads automatically -- a deliberate
    refusal (:func:`service.tts.kokoro_paths`), since a first spoken word that
    silently pulls a third of a gigabyte is a surprise on a metered connection
    and a hang on a hot path. The cost of that refusal used to be paid
    invisibly: with no weights the client fell through to the webview's own
    ``speechSynthesis``, and the macOS Compact voice became what Ada sounded
    like, with nothing anywhere saying it was a fallback.

    The client no longer falls through -- an unprovisioned engine now means
    silence -- and silence needs a place to explain itself. This is that
    place, so the wizard can say "no voice yet, here is the one command"
    rather than leaving a person to wonder why the app stopped talking.

    The states are read straight off :func:`service.tts.speak_report` rather
    than re-derived, for the reason ``google_status`` copies its state out of
    ``integrations_report``: two answers to "is the voice ready" that are
    computed separately will eventually disagree, and then neither is
    trustworthy. ``ready`` here means the same thing it means everywhere else
    in this file -- a configuration claim, not a live round trip. No audio is
    synthesized to answer this route.
    """
    from . import tts as _tts

    report = _tts.speak_report(_tts.build_engines())
    engines = report.get("engines", [])
    selected = report.get("selected")
    by_name = {e.get("name"): e for e in engines}

    if selected:
        chosen = by_name.get(selected, {})
        return {
            "state": "ready",
            "detail": f"{selected}: {chosen.get('detail', '')}",
            "selected": selected,
            "engines": engines,
            "hints": [],
            "connect": {"kind": "none"},
            "demo": False,
        }

    # Nothing selected. `unconfigured` rather than `unavailable` whenever the
    # gap is something a person can close on this machine -- which, for
    # Kokoro, it always is: the weights are a download away and the licence
    # has no commercial question attached. `unavailable` is reserved for the
    # case where even that is not true.
    kokoro = by_name.get("kokoro", {})
    hint = kokoro.get("hint", "")
    hints = [h for h in ("run ./scripts/install_voice.sh", hint) if h]
    return {
        "state": "unconfigured",
        "detail": (
            "no voice is provisioned, so Hardy will stay silent rather than "
            "fall back to the platform's robot voice"
        ),
        "selected": None,
        "engines": engines,
        "hints": hints,
        "connect": {"kind": "none"},
        "demo": False,
    }


# ---------------------------------------------------------------- google

_GOOGLE_CONNECT = {
    "kind": "oauth_browser",
    "start": "POST /setup/google/connect",
    "poll": "GET /setup/google",
    "credentials": "POST /setup/google/credentials",
    "disconnect": "POST /setup/google/disconnect",
}


def _integrations_entry(ident: str) -> dict[str, Any]:
    for entry in _integrations.integrations_report()["integrations"]:
        if entry["id"] == ident:
            return entry
    raise KeyError(ident)  # pragma: no cover - the roster is frozen


def google_status() -> dict[str, Any]:
    if is_demo():
        values, hints = _demo_values("google")
        signed_in = values.get("GOOGLE_SIGNED_IN") == "1"
        client = signed_in or values.get("GOOGLE_OAUTH_CLIENT") == "recorded"
        with _lock:
            job = _demo_jobs.get("google")
        job_status = job.status() if job else {"state": "idle", "error": None}
        if signed_in:
            state, detail = "ready", GOOGLE_DEMO_DETAIL
        elif client or job_status["state"] == "waiting":
            state, detail = "partial", "Demo: no sign-in recorded yet"
        else:
            state, detail = "unconfigured", "Demo: nothing recorded yet"
        return {
            "state": state,
            "detail": detail,
            "demo": True,
            "oauth_client": client,
            "signed_in": signed_in,
            "token": "demo" if signed_in else "missing",
            "token_path": str(_envfiles.env_path("google", demo=True)),
            "job": job_status,
            "hints": hints,
            "connect": dict(_GOOGLE_CONNECT),
        }

    report = _deliver.config_report()
    entry = _integrations_entry("google")
    _, file_hints = _live_values("google")
    return {
        # Copied, not re-derived: /setup and /integrations must agree.
        "state": entry["state"],
        "detail": entry["detail"],
        "demo": False,
        "oauth_client": bool(report.get("oauth_client")),
        "signed_in": bool(report.get("signed_in")),
        "token": report.get("token", "missing"),
        "token_path": report.get("token_path"),
        "job": _deliver.auth_status(),
        "hints": [str(h) for h in report.get("hints", [])] + file_hints,
        "connect": dict(_GOOGLE_CONNECT),
    }


def google_connect(base_url: str) -> tuple[int, dict[str, Any]]:
    """202 with the URL the client opens; 409 while one is waiting."""
    if is_demo():
        with _lock:
            current = _demo_jobs.get("google")
            if current and current.state == "waiting" and not current.expired():
                return 409, {
                    "error": "a demo sign-in is already waiting",
                    "job": current.status(),
                }
            job = DemoJob("google")
            _demo_jobs["google"] = job
        query = urllib.parse.urlencode({"provider": "google", "state": job.nonce})
        return 202, {
            "auth_url": f"{base_url.rstrip('/')}{CONSENT_ROUTE}?{query}",
            "job": job.status(),
        }
    try:
        started = _deliver.begin_auth()
    except ValueError as exc:
        text = str(exc)
        if "already in progress" in text:
            return 409, {"error": text, "job": _deliver.auth_status()}
        return 400, {"error": text}
    return 202, {"auth_url": started["auth_url"], "job": _deliver.auth_status()}


def google_disconnect() -> tuple[int, dict[str, Any]]:
    if is_demo():
        body = _disconnect_demo("google", "google")
        body["note"] = "Demo sign-in forgotten. No Google account was contacted."
        return 200, body
    try:
        out = _deliver.sign_out()
    except ValueError as exc:
        return 400, {"error": str(exc)}
    body = _disconnect_live("google")
    if out["removed"]:
        body["removed"].insert(0, "token")
    body["note"] = _deliver.SIGN_OUT_NOTE
    _deliver.cancel_auth()
    return 200, body


def google_credentials(payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    """Shape-check the OAuth client and keep it; it is proven at sign-in."""
    client_id = str(payload.get("client_id") or "").strip()
    client_secret = str(payload.get("client_secret") or "").strip()
    webhook = str(payload.get("chat_webhook") or "").strip()
    if not client_id.endswith(_GOOGLE_CLIENT_SUFFIX) or len(client_id) <= len(
        _GOOGLE_CLIENT_SUFFIX
    ):
        return 400, {
            "saved": False,
            "error": f"client_id must end with {_GOOGLE_CLIENT_SUFFIX}",
        }
    if not client_secret:
        return 400, {"saved": False, "error": "client_secret is required"}
    if webhook:
        try:
            from googleapps.chat import validate_webhook
            from googleapps.transport import GoogleError

            validate_webhook(webhook)
        except ImportError:
            return 400, {
                "saved": False,
                "error": "the googleapps package is not installed",
            }
        except GoogleError as exc:
            return 400, {"saved": False, "error": f"chat_webhook: {exc.detail}"}

    if is_demo():
        values, _ = _demo_values("google")
        kept = {k: v for k, v in values.items() if k == "GOOGLE_SIGNED_IN"}
        _write_demo("google", {**kept, "GOOGLE_OAUTH_CLIENT": "recorded"})
        return 200, {
            "saved": True,
            "verified": False,
            "note": "Demo: client id accepted by shape. No secret was stored and "
            "Google was not contacted.",
            "active": False,
            "active_source": "file",
            "report": google_status(),
        }

    values = {
        "GOOGLEAPPS_CLIENT_ID": client_id,
        "GOOGLEAPPS_CLIENT_SECRET": client_secret,
    }
    if webhook:
        values["GOOGLEAPPS_CHAT_WEBHOOK"] = webhook
    try:
        body = _save_live("google", values)
    except _envfiles.EnvFileError as exc:
        return 400, {"saved": False, "error": str(exc)}
    except OSError as exc:
        return 500, {"saved": False, "error": f"could not write: {exc.strerror}"}
    body.update(
        saved=True,
        verified=False,
        note="The OAuth client is checked by shape only; it is proven at "
        "sign-in. " + body.get("note", ""),
        report=google_status(),
    )
    body["note"] = body["note"].strip()
    return 200, body


# ---------------------------------------------------------------- microsoft

_MICROSOFT_CONNECT = {
    "kind": "credentials",
    "save": "POST /setup/microsoft/credentials",
    "poll": "GET /setup/microsoft",
    "disconnect": "POST /setup/microsoft/disconnect",
}
_MICROSOFT_KEYS = ("TEAMS_APP_ID", "TEAMS_APP_SECRET", "TEAMS_TENANT_ID")
_REQUIRED_MS = "app_id, app_secret and tenant_id are all required"


def microsoft_status() -> dict[str, Any]:
    if is_demo():
        values, hints = _demo_values("microsoft")
        accepted = values.get("MICROSOFT_ACCEPTED") == "1"
        return {
            "state": "ready" if accepted else "unconfigured",
            "detail": (
                MICROSOFT_DEMO_MEANING if accepted else "Demo: nothing recorded yet"
            ),
            "demo": True,
            "fields": [
                {"key": key, "set": accepted, "shown": "<demo>" if accepted else ""}
                for key in _MICROSOFT_KEYS
            ],
            "verified_at": values.get("KALEO_ACCEPTED_AT") if accepted else None,
            "label": "Demo" if accepted else None,
            "meaning": MICROSOFT_DEMO_MEANING,
            "unverified": True,
            "hints": hints,
            "connect": dict(_MICROSOFT_CONNECT),
        }

    present = {key: (os.environ.get(key) or "").strip() for key in _MICROSOFT_KEYS}
    fields = [
        {"key": key, "set": bool(value), "shown": _mask(value)}
        for key, value in present.items()
    ]
    count = sum(1 for value in present.values() if value)
    if count == len(_MICROSOFT_KEYS):
        state = "ready"
        detail = "app id, secret and tenant are set"
    elif count:
        state = "partial"
        detail = f"{count} of {len(_MICROSOFT_KEYS)} settings set"
    else:
        state = "unconfigured"
        detail = "no app registration"
    _, hints = _live_values("microsoft")
    with _lock:
        verified_at = _ms_verified_at
    return {
        "state": state,
        "detail": detail,
        "demo": False,
        "fields": fields,
        "verified_at": verified_at,
        "label": "Token issued" if verified_at else None,
        "meaning": MICROSOFT_MEANING,
        "unverified": True,
        "hints": hints,
        "connect": dict(_MICROSOFT_CONNECT),
    }


def _verdict(
    ok: bool, meaning: str, *, status: int | None = None, code: str = ""
) -> dict[str, Any]:
    return {
        "ok": ok,
        "status": status,
        "code": code,
        "meaning": meaning,
        "reason": meaning,
        "expires_in": None,
    }


def microsoft_credentials(payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    """Verify with Entra (live) or by shape (demo), then persist. Verify first."""
    global _ms_verified_at
    app_id = str(payload.get("app_id") or "").strip()
    app_secret = str(payload.get("app_secret") or "").strip()
    tenant_id = str(payload.get("tenant_id") or "").strip()
    verify_only = bool(payload.get("verify_only"))
    meaning = MICROSOFT_DEMO_MEANING if is_demo() else MICROSOFT_MEANING

    if not (app_id and app_secret and tenant_id):
        return 400, {
            "saved": False,
            "verdict": _verdict(False, _REQUIRED_MS),
            "meaning": meaning,
        }

    if is_demo():
        if not (_GUIDISH.match(app_id) and _GUIDISH.match(tenant_id)):
            return 400, {
                "saved": False,
                "verdict": _verdict(False, "app_id and tenant_id must be GUIDs"),
                "meaning": meaning,
            }
        verdict = _verdict(True, MICROSOFT_DEMO_MEANING)
        if verify_only:
            return 200, {"saved": False, "verdict": verdict, "meaning": meaning}
        _write_demo(
            "microsoft", {"MICROSOFT_ACCEPTED": "1", "KALEO_ACCEPTED_AT": _now_iso()}
        )
        return 200, {
            "saved": True,
            "verdict": verdict,
            "meaning": meaning,
            "label": "Demo",
            "active": False,
            "active_source": "file",
            "report": microsoft_status(),
        }

    from teamsbot.config import Config, ConfigError
    from teamsbot.graph import verify_credentials

    try:
        config = Config(app_id=app_id, app_secret=app_secret, tenant_id=tenant_id)
    except ConfigError:
        return 400, {
            "saved": False,
            "verdict": _verdict(False, _REQUIRED_MS),
            "meaning": meaning,
        }
    transport = microsoft_transport_factory() if microsoft_transport_factory else None
    result = verify_credentials(config, transport)
    verdict = result.as_dict()
    verdict["reason"] = verdict["meaning"]
    if not result.ok:
        return 400, {"saved": False, "verdict": verdict, "meaning": meaning}
    if verify_only:
        return 200, {"saved": False, "verdict": verdict, "meaning": meaning}

    values = {
        "TEAMS_APP_ID": app_id,
        "TEAMS_APP_SECRET": app_secret,
        "TEAMS_TENANT_ID": tenant_id,
    }
    try:
        body = _save_live("microsoft", values)
    except _envfiles.EnvFileError as exc:
        return 400, {
            "saved": False,
            "verdict": verdict,
            "meaning": meaning,
            "error": str(exc),
        }
    except OSError as exc:
        return 500, {"saved": False, "error": f"could not write: {exc.strerror}"}
    with _lock:
        _ms_verified_at = _now_iso()
    body.update(
        saved=True,
        verdict=verdict,
        meaning=meaning,
        label="Token issued",
        report=microsoft_status(),
    )
    return 200, body


def microsoft_disconnect() -> tuple[int, dict[str, Any]]:
    global _ms_verified_at
    if is_demo():
        return 200, _disconnect_demo("microsoft", "microsoft")
    body = _disconnect_live("microsoft")
    with _lock:
        _ms_verified_at = None
    return 200, body


# ---------------------------------------------------------------- stripe

_STRIPE_KEYS = ("STRIPE_API_KEY", "STRIPE_WEBHOOK_SECRET", "STRIPE_PRICE_ID")
_STRIPE_CONNECT = {
    "kind": "credentials",
    "save": "POST /setup/stripe/credentials",
    "status": "GET /billing/config",
    "oauth": "POST /billing/connect",
}


def stripe_status() -> dict[str, Any]:
    if is_demo():
        values, hints = _demo_values("billing")
        ready = values.get("STRIPE_KEY_MODE") == "demo"
        return {
            "state": "ready" if ready else "unconfigured",
            "detail": "Demo: the demo key was accepted; no charge can happen"
            if ready
            else "Demo: nothing recorded yet",
            "demo": True,
            "ready": ready,
            "key_mode": "demo",
            "steps": [
                {
                    "key": key,
                    "present": ready,
                    "preview": "<demo>" if ready else None,
                    "warning": None,
                }
                for key in _STRIPE_KEYS
            ],
            "demo_key": DEMO_STRIPE_KEY,
            "hints": hints,
            # The OAuth consent reaches access.stripe.com; not in demo.
            "connect": {**_STRIPE_CONNECT, "oauth": None},
        }

    _, report = _billing.handle_config_get()
    steps = [
        {
            "key": step["key"],
            "present": step["present"],
            "preview": step["preview"],
            "warning": step["warning"],
        }
        for step in report["steps"]
    ]
    present = sum(1 for step in steps if step["present"])
    if report["ready"]:
        state, detail = "ready", STRIPE_LIVE_NOTE
    elif present:
        state = "partial"
        detail = f"{present} of {len(steps)} settings set"
    else:
        state, detail = "unconfigured", "no Stripe key"
    hints = []
    mode_hint = report.get("storage", {}).get("mode_hint")
    if mode_hint:
        hints.append(mode_hint)
    return {
        "state": state,
        "detail": detail,
        "demo": False,
        "ready": bool(report["ready"]),
        "key_mode": report["mode"],
        "steps": steps,
        "hints": hints,
        "connect": dict(_STRIPE_CONNECT),
    }


def stripe_credentials(payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    """Delegate to ``billing_routes.handle_config_post`` -- live or demo."""
    if not is_demo():
        status, body = _billing.handle_config_post(dict(payload))
        if body.get("saved"):
            body["note"] = (STRIPE_LIVE_NOTE + " " + body.get("note", "")).strip()
            body["report"] = stripe_status()
        return status, body

    key = str(payload.get("STRIPE_API_KEY") or "").strip()
    if "_live_" in key:
        return 400, {"saved": False, "error": "live keys are never accepted"}
    if key != DEMO_STRIPE_KEY:
        if "_test_" in key and len(key) >= 20:
            return 400, {
                "saved": False,
                "error": "that looks like a real test key; demo mode does not "
                "store real keys",
            }
        return 400, {
            "saved": False,
            "error": f"demo mode accepts only the demo key {DEMO_STRIPE_KEY}",
        }
    # Verified through the same function the live path uses, against a
    # transport that never leaves the process; nothing is saved by it
    # (verify_only) and nothing is exported.
    status, body = _billing.handle_config_post(
        {"STRIPE_API_KEY": key, "verify_only": True},
        transport=demo_stripe_transport_factory(),
        apply_env=False,
    )
    if status != 200 or not body.get("check", {}).get("ok"):
        return 400, {"saved": False, "check": body.get("check")}
    _write_demo("billing", {"STRIPE_KEY_MODE": "demo"})
    return 200, {
        "saved": True,
        "check": body["check"],
        "key_mode": "demo",
        "note": "Demo: the demo key was accepted. No key was stored and Stripe "
        "was not contacted.",
        "active": False,
        "active_source": "file",
        "report": stripe_status(),
    }


# ---------------------------------------------------------------- report


def setup_report() -> dict[str, Any]:
    """The whole thing. One provider raising costs that provider, not the route."""
    body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "home": str(_envfiles.kaleo_home()),
        "demo_home": str(_envfiles.demo_dir()),
        "banner": DEMO_BANNER if is_demo() else None,
        "engine": engine_status(),
    }
    for ident, probe in (
        ("google", google_status),
        ("microsoft", microsoft_status),
        ("stripe", stripe_status),
        ("voice", voice_status),
    ):
        try:
            body[ident] = probe()
        except Exception as exc:  # noqa: BLE001 - one bad probe, not a 500
            body[ident] = _unavailable(ident, exc)
    return _stamp(body)


# ---------------------------------------------------------------- consent


_PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
body{{font:15px/1.5 -apple-system,system-ui,sans-serif;margin:0;
background:#f5f5f7;color:#1d1d1f}}
main{{max-width:420px;margin:12vh auto;padding:32px;background:#fff;
border-radius:16px;box-shadow:0 2px 12px rgba(0,0,0,.08)}}
h1{{font-size:20px;margin:0 0 8px}}p{{margin:0 0 16px;color:#515154}}
.demo{{display:inline-block;font-size:12px;padding:2px 8px;border-radius:999px;
background:#fff3cd;color:#7a5b00;margin-bottom:16px}}
button{{font:inherit;padding:10px 18px;border-radius:10px;
border:1px solid #d2d2d7;background:#fff;cursor:pointer}}
button.allow{{background:#0071e3;color:#fff;border-color:#0071e3;margin-right:8px}}
</style>
<main>
<span class="demo">Demo</span>
<h1>{title}</h1>
{body}
</main>
"""


def _page(title: str, body_html: str) -> str:
    return _PAGE.format(title=html.escape(title), body=body_html)


def _invalid_page() -> str:
    return _page(
        "This link is not valid",
        "<p>The demo consent link is missing, already used, or older than ten "
        "minutes. Go back to Hardy and press Connect again.</p>",
    )


def demo_consent_page(query: Mapping[str, str]) -> tuple[int, str]:
    """The pretend consent screen. 404 for anything but a live nonce."""
    provider = str(query.get("provider") or "")
    state = str(query.get("state") or "")
    if provider != "google":
        return 404, _invalid_page()
    with _lock:
        job = _demo_jobs.get(provider)
        ok = job is not None and job.matches(state)
    if not ok:
        return 404, _invalid_page()
    form = (
        f'<p>Hardy is asking to connect a <strong>demo</strong> Google account. '
        f"No real Google account is involved and nothing leaves this Mac.</p>"
        f'<form method="post" action="{CONSENT_ROUTE}">'
        f'<input type="hidden" name="provider" value="{html.escape(provider)}">'
        f'<input type="hidden" name="state" value="{html.escape(state)}">'
        f'<button class="allow" name="decision" value="allow">Allow</button>'
        f'<button name="decision" value="deny">Deny</button>'
        f"</form>"
    )
    return 200, _page("Connect Google (demo)", form)


def demo_consent_decide(form: Mapping[str, str]) -> tuple[int, str]:
    """Spend the nonce and flip the job. The tab can then be closed."""
    provider = str(form.get("provider") or "")
    state = str(form.get("state") or "")
    decision = str(form.get("decision") or "")
    if provider != "google":
        return 404, _invalid_page()
    if decision not in ("allow", "deny"):
        return 400, _page("Bad request", "<p>decision must be allow or deny.</p>")
    with _lock:
        job = _demo_jobs.get(provider)
        if job is None or not job.matches(state):
            return 404, _invalid_page()
        job.used = True
        if decision == "allow":
            job.state, job.error = "connected", None
        else:
            job.state, job.error = "failed", "you declined the demo sign-in"
    if decision == "allow":
        values, _ = _demo_values("google")
        _write_demo(
            "google",
            {
                "GOOGLE_OAUTH_CLIENT": values.get("GOOGLE_OAUTH_CLIENT") or "recorded",
                "GOOGLE_SIGNED_IN": "1",
            },
        )
        return 200, _page(
            "Demo sign-in recorded",
            "<p>You can close this tab. No Google account was contacted.</p>",
        )
    return 200, _page(
        "Demo sign-in declined",
        "<p>You can close this tab. Nothing was recorded.</p>",
    )


# ---------------------------------------------------------------- routing


def _route(path: str) -> str:
    route = urllib.parse.urlsplit(path).path
    return route.rstrip("/") or "/"


def handle_get(
    path: str,
    query: Mapping[str, str] | None = None,
    *,
    host: str | None = None,
    server_host: str | None = None,
) -> tuple[int, Any, str]:
    """``(status, body, content_type)``: a dict for JSON, a str for HTML."""
    route = _route(path)
    query = dict(query or {})
    if route == CONSENT_ROUTE:
        if not is_demo():
            return 404, _stamp({"error": f"no route {route}"}), "application/json"
        if not host_is_local(host, server_host):
            return 403, _stamp({"error": _LOOPBACK_ONLY}), "application/json"
        status, page = demo_consent_page(query)
        return status, page, "text/html"
    if route == "/setup":
        return 200, setup_report(), "application/json"
    if route == "/setup/google":
        return 200, _stamp(google_status()), "application/json"
    if route == "/setup/microsoft":
        return 200, _stamp(microsoft_status()), "application/json"
    if route == "/setup/stripe":
        return 200, _stamp(stripe_status()), "application/json"
    if route == "/setup/voice":
        return 200, _stamp(voice_status()), "application/json"
    return 404, _stamp({"error": f"no route {route}"}), "application/json"


def handle_post(
    path: str,
    payload: Mapping[str, Any] | None,
    *,
    base_url: str,
    form: Mapping[str, str] | None = None,
    host: str | None = None,
    server_host: str | None = None,
) -> tuple[int, Any, str]:
    """``(status, body, content_type)``; the consent POST is the only HTML."""
    route = _route(path)
    payload = dict(payload or {})
    if route == CONSENT_ROUTE:
        if not is_demo():
            return 404, _stamp({"error": f"no route {route}"}), "application/json"
        if not host_is_local(host, server_host):
            return 403, _stamp({"error": _LOOPBACK_ONLY}), "application/json"
        if form is None:
            return 400, _stamp({"error": "expected a form"}), "application/json"
        status, page = demo_consent_decide(form)
        return status, page, "text/html"

    handlers: dict[str, Callable[[], tuple[int, dict[str, Any]]]] = {
        "/setup/google/connect": lambda: google_connect(base_url),
        "/setup/google/disconnect": google_disconnect,
        "/setup/google/credentials": lambda: google_credentials(payload),
        "/setup/microsoft/credentials": lambda: microsoft_credentials(payload),
        "/setup/microsoft/disconnect": microsoft_disconnect,
        "/setup/stripe/credentials": lambda: stripe_credentials(payload),
    }
    handler = handlers.get(route)
    if handler is None:
        return 404, _stamp({"error": f"no route {route}"}), "application/json"
    status, body = handler()
    return status, _stamp(body), "application/json"
