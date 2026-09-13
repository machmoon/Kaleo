"""What this service could talk to right now -- ``GET /integrations``.

One read-only, aggregated, always-200 view of every front end and optional
tool the repo carries: the Workspace delivery routes, the Slack bot, the Meet
/ Zoom / Teams meeting front ends, the enclosure kernel, the sourcing BOM, the
MCP server, SPICE and ``kicad-cli``. The desktop overlay renders it as a
settings panel, so the contract is that it never fails and never lies.

Four states, and the distinction is the whole point (a missing package, a
missing credential and a half-filled one are three different problems):

``unavailable``
    The code is not here at all -- an optional extra is not installed, a
    binary is not on PATH, a package has not been written yet. ``hints`` names
    the extra or says the feature is not built.
``unconfigured``
    Importable, nothing set.
``partial``
    Some required settings present, some missing (or one is malformed).
    ``hints`` names each gap **and** says the service does not read ``.env``,
    because the service genuinely does not (only the two CLIs do) and a hint
    that names a variable without saying so sends the reader to a file nothing
    opens.
``ready``
    Everything required is present. This is a claim about *configuration* and
    nothing else: no route here makes a live call, so ``detail`` must never
    read as "a round trip succeeded".

Two rules hold everywhere. **No secret is ever echoed** -- not a value, not a
tail (a webhook URL's tail is the token); ``shown`` carries the
``<set, N chars>`` mask ``slackbot.config.Config.redacted`` uses, and only a
setting that is not a credential (a path, a base URL, an allowlist) shows its
value. And **nothing raises out of this module**: a probe that blows up
becomes that one integration's ``state`` and ``hints``, never a 500 that hides
the nine integrations that were fine.

The ``google`` entry is derived from :func:`service.deliver.config_report`
rather than re-deriving it, so ``GET /integrations`` and ``GET
/deliver/config`` cannot disagree about whether Ada can send mail.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from typing import Any

from . import deliver as _deliver

__all__ = ["SCHEMA_VERSION", "INTEGRATION_IDS", "integrations_report"]

SCHEMA_VERSION = 1

#: The frozen roster (``docs/integrations-plan.md``). Every id appears in every
#: response, in this order -- an integration that is not built yet is reported
#: ``unavailable`` rather than omitted, because unbuilt work stays visibly
#: unbuilt.
INTEGRATION_IDS = (
    "google",
    "slack",
    "meet",
    "zoom",
    "teams",
    "cad",
    "sourcing",
    "mcp",
    "spice",
    "kicad",
)

#: ``service/deliver.py`` owns this wording; borrowed rather than copied so the
#: two routes phrase the same fact the same way. The literal is the fallback
#: for the day that module is refactored -- a missing hint would be worse than
#: a duplicated sentence.
_ENV_NOTE = getattr(
    _deliver,
    "_ENV_NOTE",
    "in the service's environment (the service does not read .env)",
)

#: ``id -> (name, kind, summary, docs, unverified)``. Held apart from the
#: probes so a probe that raises still produces a complete, correctly-labelled
#: entry.
_META: dict[str, tuple[str, str, str, str, bool]] = {
    # ``unverified`` is true for the same reason it is true of ``slack``, and
    # it matters more here: this is the integration that sends real email to
    # real people. README's module table says googleapps is "untested against
    # live Google APIs" and CLAUDE.md says it was "built offline against the
    # documented REST surface and never run against live Google APIs". Marking
    # it verified because it is the oldest and most finished of the delivery
    # surfaces would be exactly the misreading this flag exists to prevent.
    "google": (
        "Google Workspace",
        "delivery",
        "Posts a finished run to Chat, mails the board, books the review.",
        "docs/googleapps.md",
        True,
    ),
    # ``unverified`` is true because README's module table says the Slack bot
    # is "untested against a live workspace". The package's own docstrings do
    # not repeat that (``zoombot/__init__.py`` does), so the README row is the
    # evidence; if the bot is ever run in a real workspace, both must change
    # together.
    "slack": (
        "Slack",
        "delivery",
        "Runs the pipeline from a Slack message and answers in the thread.",
        "README.md#in-slack",
        True,
    ),
    "meet": (
        "Google Meet",
        "meeting",
        "Reads a finished meeting's transcript and builds what was asked for.",
        "README.md#in-google-meet",
        True,
    ),
    "zoom": (
        "Zoom",
        "meeting",
        "Joins a Zoom meeting, listens, and reports what it would build.",
        "docs/zoom.md",
        True,
    ),
    "teams": (
        "Microsoft Teams",
        "meeting",
        "Joins a Teams meeting, listens, and reports what it would build.",
        "docs/teams.md",
        True,
    ),
    "cad": (
        "Enclosure kernel",
        "design",
        "Builds a real B-rep case for the board and measures it against the "
        "acceptance clauses.",
        "docs/ai-cad-plan.md",
        False,
    ),
    "sourcing": (
        "Sourcing BOM",
        "fabrication",
        "Proposes a manufacturer, part number and datasheet for every placed "
        "part.",
        "README.md",
        False,
    ),
    "mcp": (
        "MCP server",
        "design",
        "Exposes the engine's operations to an MCP client over stdio.",
        "README.md#as-an-mcp-server",
        False,
    ),
    "spice": (
        "SPICE",
        "design",
        "Simulates the proposed circuit and checks it against a specification.",
        "README.md",
        False,
    ),
    "kicad": (
        "KiCad CLI",
        "fabrication",
        "Exports the 3D model of an ordered board and runs DRC/ERC locally.",
        "docs/install.md",
        False,
    ),
}

_STATES = ("ready", "partial", "unconfigured", "unavailable")
_KINDS = ("delivery", "meeting", "design", "fabrication")

#: A value longer than this is truncated before it is shown. Only non-secret
#: settings are ever shown at all, but an allowlist can still be long enough to
#: wreck a card.
_SHOWN_MAX = 120


# ---------------------------------------------------------------- primitives


def _text(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "")
    return value.strip() if isinstance(value, str) else ""


def _setting(
    env: Mapping[str, str],
    key: str,
    *,
    required: bool = True,
    secret: bool = True,
    note: str = "",
) -> dict[str, Any]:
    """One settings row. ``shown`` is a mask for anything credential-shaped.

    ``secret=True`` is the default on purpose: a row nobody thought about
    hides its value rather than printing it.
    """
    raw = _text(env, key)
    if not raw:
        shown = ""
    elif secret:
        shown = f"<set, {len(raw)} chars>"
    elif len(raw) > _SHOWN_MAX:
        shown = raw[: _SHOWN_MAX - 1] + "…"
    else:
        shown = raw
    return {
        "key": key,
        "required": required,
        "set": bool(raw),
        "shown": shown,
        "note": note,
    }


def _missing(settings: list[dict[str, Any]]) -> list[str]:
    return [s["key"] for s in settings if s["required"] and not s["set"]]


def _env_hints(keys: list[str]) -> list[str]:
    return [f"Set {key} {_ENV_NOTE}." for key in keys]


def _state_for(settings: list[dict[str, Any]]) -> str:
    """``ready`` / ``partial`` / ``unconfigured`` from required rows alone."""
    required = [s for s in settings if s["required"]]
    if not required:
        return "ready"
    present = [s for s in required if s["set"]]
    if len(present) == len(required):
        return "ready"
    # An optional row that is set still counts as "somebody started this".
    started = bool(present) or any(s["set"] for s in settings if not s["required"])
    return "partial" if started else "unconfigured"


def _entry(
    ident: str,
    *,
    state: str,
    detail: str,
    settings: list[dict[str, Any]] | None = None,
    hints: list[str] | None = None,
    actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Assemble one roster entry in the frozen shape."""
    name, kind, summary, docs, unverified = _META[ident]
    if state not in _STATES:  # pragma: no cover - guards a typo, not a caller
        raise ValueError(f"unknown state {state!r}")
    if kind not in _KINDS:  # pragma: no cover - same
        raise ValueError(f"unknown kind {kind!r}")
    return {
        "id": ident,
        "name": name,
        "kind": kind,
        "state": state,
        "summary": summary,
        "detail": detail,
        "settings": list(settings or []),
        "hints": list(hints or []),
        "actions": list(actions or []),
        "docs": docs,
        "unverified": unverified,
    }


def _probe_failed(ident: str, exc: BaseException) -> dict[str, Any]:
    """The last line of defence: a probe raised, and the route still answers."""
    return _entry(
        ident,
        state="unavailable",
        detail="this integration could not be inspected",
        hints=[f"checking {ident} raised {type(exc).__name__}: {exc}"],
    )


# ---------------------------------------------------------------- probes


def _google(env: Mapping[str, str], explicit: bool) -> dict[str, Any]:
    """Whatever ``GET /deliver/config`` would say, in the roster's shape.

    The report is the authority. Nothing here re-reads a Google variable to
    decide a state, so the two routes cannot drift apart; the settings rows
    are presentation only.
    """
    report = _google_report(env, explicit)
    settings = [
        _setting(env, "GOOGLEAPPS_CHAT_WEBHOOK", required=False),
        _setting(env, "GOOGLEAPPS_CLIENT_ID", required=False),
        _setting(env, "GOOGLEAPPS_CLIENT_SECRET", required=False),
        _setting(env, "GOOGLEAPPS_TOKEN_PATH", required=False, secret=False),
    ]
    hints = [str(h) for h in report.get("hints", [])]
    actions: list[dict[str, Any]] = []

    if not report.get("available"):
        return _entry(
            "google",
            state="unavailable",
            detail="the googleapps package is not installed beside the service",
            settings=settings,
            hints=hints,
        )

    ready = [
        label
        for label, ok in (
            ("Chat", report.get("chat")),
            ("Gmail", report.get("gmail")),
            ("Calendar", report.get("calendar")),
        )
        if ok
    ]
    if report.get("oauth_client"):
        actions.append(
            {
                "id": "google_sign_in",
                "label": "Sign in with Google",
                "method": "POST",
                "path": "/deliver/auth/start",
            }
        )

    if len(ready) == 3:
        state = "ready"
        detail = "Chat, Gmail and Calendar are configured (nothing has been sent)"
    elif ready:
        state = "partial"
        detail = f"configured: {', '.join(ready)}; the rest is not"
    elif report.get("oauth_client") or report.get("token") != "missing":
        state = "partial"
        detail = f"OAuth client present, token {report.get('token')}"
    else:
        state = "unconfigured"
        detail = "no webhook and no OAuth client"
    return _entry(
        "google",
        state=state,
        detail=detail,
        settings=settings,
        hints=hints,
        actions=actions,
    )


def _google_report(env: Mapping[str, str], explicit: bool) -> dict[str, Any]:
    """``deliver.config_report`` over the environment this call was given."""
    if not explicit:
        return _deliver.config_report()
    try:
        from googleapps.config import load_config
    except ImportError:
        # No package: config_report answers "unavailable" whatever the env is.
        return _deliver.config_report()
    return _deliver.config_report(load_config(dict(env), dotenv=False))


def _slack(env: Mapping[str, str], explicit: bool) -> dict[str, Any]:
    settings = [
        _setting(env, "SLACK_BOT_TOKEN"),
        _setting(env, "SLACK_SIGNING_SECRET"),
        _setting(
            env,
            "GOOGLE_API_KEY",
            note="every Slack run is a pipeline run, so the bot refuses to "
            "start without a model key",
        ),
        _setting(env, "SILKSCREEN_SLACK_CHANNELS", required=False, secret=False),
        _setting(env, "SILKSCREEN_SLACK_PORT", required=False, secret=False),
        _setting(env, "SILKSCREEN_SLACK_WORKDIR", required=False, secret=False),
        _setting(env, "SILKSCREEN_MODEL", required=False, secret=False),
    ]
    try:
        from slackbot.config import ConfigError, load_config
    except Exception as exc:  # noqa: BLE001 - an absent front end is a state
        return _entry(
            "slack",
            state="unavailable",
            detail="the slackbot package is not importable here",
            settings=settings,
            hints=[f"slackbot could not be imported: {type(exc).__name__}: {exc}"],
        )

    try:
        config = load_config(dict(env), dotenv=False)
    except ConfigError as exc:
        # ConfigError names every gap at once; keep that batch, and add the
        # per-variable hint the panel can act on.
        missing = _missing(settings)
        hints = _env_hints(missing) if missing else [str(exc)]
        return _entry(
            "slack",
            state=_state_for(settings),
            detail="not ready to start: " + str(exc),
            settings=settings,
            hints=hints,
        )

    channels = len(config.allowed_channels)
    where = f"{channels} channel(s) allowlisted" if channels else "any channel"
    return _entry(
        "slack",
        state="ready",
        detail=(
            f"bot token and signing secret set; {where}; port {config.port} "
            "(nothing has been posted)"
        ),
        settings=settings,
    )


def _meet(env: Mapping[str, str], explicit: bool) -> dict[str, Any]:
    settings = [
        _setting(
            env,
            "MEET_ACCESS_TOKEN",
            note="this package does not perform the OAuth dance; the host "
            "supplies a read-only token",
        ),
        _setting(env, "MEET_SPACES", required=False, secret=False),
        _setting(env, "MEET_API_BASE", required=False, secret=False),
        _setting(env, "MEET_MAX_AGE_HOURS", required=False, secret=False),
        _setting(env, "MEET_MAX_RUNS_PER_POLL", required=False, secret=False),
    ]
    try:
        from meetings.config import ConfigError, MeetConfig
    except Exception as exc:  # noqa: BLE001
        return _entry(
            "meet",
            state="unavailable",
            detail="the meetings package is not importable here",
            settings=settings,
            hints=[f"meetings could not be imported: {type(exc).__name__}: {exc}"],
        )

    try:
        config = MeetConfig.from_env(dict(env))
    except ConfigError as exc:
        missing = _missing(settings)
        hints = _env_hints(missing) if missing else [str(exc)]
        return _entry(
            "meet",
            state=_state_for(settings),
            detail="not ready to poll: " + str(exc),
            settings=settings,
            hints=hints,
        )

    spaces = len(config.space_allowlist)
    scope = f"{spaces} space(s) allowlisted" if spaces else "every visible conference"
    return _entry(
        "meet",
        state="ready",
        detail=(
            f"token set; {scope}; at most {config.max_runs_per_poll} run(s) per "
            "poll (no conference has been read)"
        ),
        settings=settings,
    )


def _meeting_bot(
    ident: str, env: Mapping[str, str], module: str, rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """Zoom and Teams: the same shape, and the same not-built-yet answer.

    Both packages are being written against frozen env names. The import is
    lazy and every failure -- absent, half-written, broken -- is this
    integration's state and nothing else's.
    """
    try:
        config_module = __import__(module, fromlist=["load_config"])
    except Exception as exc:  # noqa: BLE001 - absence is the expected case
        return _entry(
            ident,
            state="unavailable",
            detail=f"the {module.split('.')[0]} package is not present here",
            settings=rows,
            hints=[
                f"{module.split('.')[0]} is not installed beside the service "
                f"({type(exc).__name__}); the integration is not built here yet"
            ],
        )

    load_config = getattr(config_module, "load_config", None)
    config_error = getattr(config_module, "ConfigError", None)
    state = _state_for(rows)
    missing = _missing(rows)
    hints = _env_hints(missing)
    detail = (
        "every required setting is present (nothing has been joined)"
        if state == "ready"
        else "missing: " + ", ".join(missing)
        if missing
        else "not configured"
    )
    if load_config is not None and isinstance(config_error, type):
        try:
            load_config(dict(env))
        except config_error as exc:
            # The package's own validator is the better witness when it exists.
            state = "partial" if state == "ready" else state
            detail = "not ready to start: " + str(exc)
            hints = hints or [str(exc)]
        except Exception as exc:  # noqa: BLE001
            state = "partial" if state == "ready" else state
            detail = f"configuration check raised {type(exc).__name__}: {exc}"
    return _entry(ident, state=state, detail=detail, settings=rows, hints=hints)


def _zoom(env: Mapping[str, str], explicit: bool) -> dict[str, Any]:
    rows = [
        _setting(env, "ZOOM_CLIENT_ID"),
        _setting(env, "ZOOM_CLIENT_SECRET"),
        _setting(env, "ZOOM_ACCOUNT_ID"),
        _setting(env, "ZOOM_WEBHOOK_SECRET_TOKEN"),
        _setting(env, "ZOOM_RTMS_ENABLED", required=False, secret=False),
        _setting(
            env,
            "ZOOM_SPEAK_MODE",
            required=False,
            secret=False,
            note="sdk | chat | off -- 'sdk' needs the headless container, "
            "which is not run here",
        ),
        _setting(env, "ZOOM_MEETINGS", required=False, secret=False),
        _setting(env, "ZOOM_MAX_RUNS_PER_MEETING", required=False, secret=False),
        _setting(env, "ZOOM_API_BASE", required=False, secret=False),
        # Landed after this list was first written. Every name in
        # ``zoombot.config.ZOOM_ENV`` appears here, and
        # ``test_integrations.py`` asserts that both ways round: a variable
        # the package reads but this route never mentions is invisible to the
        # panel, and one this route names but the package ignores is dead
        # config the reader will set and wonder about.
        _setting(env, "ZOOM_PORT", required=False, secret=False),
    ]
    return _meeting_bot("zoom", env, "zoombot.config", rows)


def _teams(env: Mapping[str, str], explicit: bool) -> dict[str, Any]:
    rows = [
        _setting(env, "TEAMS_APP_ID"),
        _setting(env, "TEAMS_APP_SECRET"),
        _setting(env, "TEAMS_TENANT_ID"),
        _setting(env, "TEAMS_BOT_ENDPOINT", required=False, secret=False),
        _setting(
            env,
            "TEAMS_SPEAK_MODE",
            required=False,
            secret=False,
            note="sdk | chat | off -- 'sdk' needs the policy-gated calling "
            "bot, which is not run here",
        ),
        _setting(env, "TEAMS_MEETINGS", required=False, secret=False),
        _setting(env, "TEAMS_MAX_RUNS_PER_MEETING", required=False, secret=False),
        _setting(env, "TEAMS_GRAPH_BASE", required=False, secret=False),
        # Landed after this list was first written, for the same reason
        # ``ZOOM_PORT`` did: the callback address registered with Microsoft
        # names a port, so which port the process binds is deployment
        # configuration. ``test_integrations.py`` asserts this list and
        # ``teamsbot.config.TEAMS_ENV`` agree both ways round.
        _setting(env, "TEAMS_PORT", required=False, secret=False),
    ]
    return _meeting_bot("teams", env, "teamsbot.config", rows)


def _cad(env: Mapping[str, str], explicit: bool) -> dict[str, Any]:
    """The ``cad`` extra, gated exactly the way the kernel tests gate.

    Without build123d the case feature does not exist: the v1 OpenSCAD
    fallback was removed on 2026-09-08 (docs/ai-cad-plan.md v3), so the state
    is ``unavailable`` and the hint names the extra.
    """
    try:
        from silkscreen.enclosure.cad import kernel_available
    except Exception as exc:  # noqa: BLE001
        return _entry(
            "cad",
            state="unavailable",
            detail="the enclosure kernel module is not importable",
            hints=[
                f"silkscreen.enclosure.cad could not be imported "
                f"({type(exc).__name__}); install the cad extra: "
                "pip install -e '.[cad]'"
            ],
        )
    if not kernel_available():
        return _entry(
            "cad",
            state="unavailable",
            detail=(
                "build123d is not installed; the case feature refuses rather "
                "than degrading -- there is no second enclosure engine"
            ),
            hints=["Install the cad extra: pip install -e '.[cad]'"],
        )
    return _entry(
        "cad",
        state="ready",
        detail="build123d/OCCT is importable; B-rep cases and the acceptance "
        "clauses are available",
    )


def _sourcing(env: Mapping[str, str], explicit: bool) -> dict[str, Any]:
    settings = [
        _setting(
            env,
            "GOOGLE_API_KEY",
            note="the sourcing stage is one model call per run",
        ),
        _setting(env, "SILKSCREEN_MODEL", required=False, secret=False),
    ]
    try:
        from silkscreen.agents.sourcing import probe_pdf  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return _entry(
            "sourcing",
            state="unavailable",
            detail="the sourcing stage is not importable",
            settings=settings,
            hints=[
                f"silkscreen.agents.sourcing could not be imported: "
                f"{type(exc).__name__}: {exc}"
            ],
        )
    state = _state_for(settings)
    if state != "ready":
        return _entry(
            "sourcing",
            state=state,
            detail="no model key, so a run gets the deterministic BOM with "
            "every status 'none'",
            settings=settings,
            hints=_env_hints(_missing(settings)),
        )
    return _entry(
        "sourcing",
        state="ready",
        detail=(
            "model key set; the datasheet probe can run (it needs outbound "
            "HTTPS, which is not checked here). An MPN is only ever "
            "'proposed' -- no distributor API exists here"
        ),
        settings=settings,
    )


def _mcp(env: Mapping[str, str], explicit: bool) -> dict[str, Any]:
    try:
        from silkscreen.mcp import server
    except Exception as exc:  # noqa: BLE001
        return _entry(
            "mcp",
            state="unavailable",
            detail="silkscreen.mcp.server is not importable",
            hints=[
                f"the engine is not installed here ({type(exc).__name__}); "
                "pip install -e '.'"
            ],
        )
    script = shutil.which("silkscreen-mcp")
    tools = getattr(server, "TOOLS", ())
    detail = f"{len(tools)} tool(s) over stdio; "
    detail += (
        f"the silkscreen-mcp console script is at {script}"
        if script
        else "the silkscreen-mcp console script is not on PATH "
        "(run it as `python -m silkscreen.mcp.server`)"
    )
    return _entry("mcp", state="ready", detail=detail)


def _spice(env: Mapping[str, str], explicit: bool) -> dict[str, Any]:
    try:
        from silkscreen.spice.errors import SimulatorNotFound
        from silkscreen.spice.simulators import find_simulator
    except Exception as exc:  # noqa: BLE001
        return _entry(
            "spice",
            state="unavailable",
            detail="the spice package is not importable",
            hints=[
                f"silkscreen.spice could not be imported: "
                f"{type(exc).__name__}: {exc}"
            ],
        )
    try:
        simulator = find_simulator()
    except SimulatorNotFound as exc:
        return _entry(
            "spice",
            state="unavailable",
            detail="no simulator binary was found",
            hints=[
                f"install ngspice (brew install ngspice / apt-get install "
                f"ngspice); tried: {exc}"
            ],
        )
    return _entry(
        "spice",
        state="ready",
        detail=f"{simulator.name} is on PATH; circuits can be verified in a loop",
    )


def _kicad(env: Mapping[str, str], explicit: bool) -> dict[str, Any]:
    from .kicad_cli import ENV_VAR, _missing_reason, find_kicad_cli

    settings = [
        _setting(
            env,
            ENV_VAR,
            required=False,
            secret=False,
            note="names the binary outright; when set it wins over PATH",
        )
    ]
    binary = find_kicad_cli(dict(env))
    if binary:
        # The version is deliberately not queried: that is a subprocess on
        # every request, and this route is polled by a settings panel.
        return _entry(
            "kicad",
            state="ready",
            detail=f"kicad-cli found at {binary} (version not queried)",
            settings=settings,
        )
    if _text(env, ENV_VAR):
        return _entry(
            "kicad",
            state="partial",
            detail=_missing_reason(dict(env)),
            settings=settings,
            hints=[f"Point {ENV_VAR} at an existing kicad-cli binary {_ENV_NOTE}."],
        )
    return _entry(
        "kicad",
        state="unavailable",
        detail="no kicad-cli on PATH or in a known install location",
        settings=settings,
        hints=[
            "Install KiCad, or set KICAD_CLI to the binary "
            f"{_ENV_NOTE}. Without it a board is ordered without its 3D model."
        ],
    )


_PROBES = {
    "google": _google,
    "slack": _slack,
    "meet": _meet,
    "zoom": _zoom,
    "teams": _teams,
    "cad": _cad,
    "sourcing": _sourcing,
    "mcp": _mcp,
    "spice": _spice,
    "kicad": _kicad,
}


# ---------------------------------------------------------------- the report


def integrations_report(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Every integration's state, right now. Never raises, never a secret.

    ``environ`` is the test seam (the ``Handler.model_factory`` convention):
    ``None`` reads the real process environment, and the ``google`` entry then
    comes from :func:`service.deliver.config_report` with no argument, which is
    exactly what ``GET /deliver/config`` answers.
    """
    explicit = environ is not None
    env = dict(os.environ if environ is None else environ)
    entries: list[dict[str, Any]] = []
    for ident in INTEGRATION_IDS:
        try:
            entries.append(_PROBES[ident](env, explicit))
        except Exception as exc:  # noqa: BLE001 - one bad probe, not a 500
            entries.append(_probe_failed(ident, exc))
    return {"schema_version": SCHEMA_VERSION, "integrations": entries}
