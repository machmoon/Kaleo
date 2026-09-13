# Integrations build — frozen contracts (2026-09-06)

> **Historical document. The build it froze is finished, and the code has moved
> past it.** Keep it for the reasoning and the citations; do **not** treat it as
> the current contract for any of these packages. The authoritative descriptions
> are `docs/zoom.md`, `docs/teams.md`, `docs/spec-review.md` and `CLAUDE.md`, plus
> the modules themselves.
>
> One concrete reason this matters rather than being tidiness: the Teams
> allowlist below (§ "Feature 3") specifies a `*.communication.azure.com`
> wildcard. `teamsbot/graph.py:77-82` deliberately does **not** include it —
> `ALLOWED_HOSTS` is `graph.microsoft.com` and `login.microsoftonline.com` only,
> with a comment saying an allowlist entry for a host nothing calls is an open
> door with no reason to be open. Following this file there would *weaken* the
> shipped code. Where the two disagree, the code is right.

Four features land in parallel. This file was the **contract every agent coded
against** during that build, and was frozen for its duration. If you believe a
contract is wrong, say so in your report — do not change it unilaterally,
because three other agents are compiling against it.

## Repo rules that apply to all of this work

Read `CLAUDE.md` first. The rules that bite hardest here:

- **Nothing returns a quiet zero.** Every failure path raises a specific error
  or is reported as a named failure. An empty result must never be readable as
  success. A missing package, a missing credential and a failed call are three
  different states and must stay distinguishable.
- **Batch validation failures.** A validator collects every problem into one
  error (`netlist.ValidationError`, `meetings.intent.IntentError.errors`), it
  does not raise on the first.
- **Every network boundary is a `Transport` Protocol seam** with a `Urllib*`
  production implementation and a recorded/fake one in tests, exactly as
  `meetings/meet.py` and `googleapps/transport.py` do. The whole suite must
  pass offline with no keys.
- **Host allowlist on every transport.** Exact host match, redirects refused
  (a 3xx carries the bearer token off-allowlist).
- **Config validates once at construction** and names everything missing at
  once (`slackbot/config.py`, `meetings/config.py`).
- **Secrets are never echoed**, not even a tail — `Config.redacted()` returns
  masks and lengths.
- **Unverified is stated, not hidden.** Anything that has never run against a
  live account says so in its module docstring and in its docs page, the way
  `meetings/` does.
- Do not touch: `pyproject.toml`, `.github/workflows/ci.yml`, `README.md`,
  `DEVPOST.md`, `TODO.txt`, `CLAUDE.md`, `docs/` (except a docs page this file
  assigns you). The coordinator wires packaging, CI and docs at the end.
- Do not run `git commit`, `git push`, `cargo`, `npm install`, or
  `pip install`. Use the existing `.venv`.
- Python: `python -m pytest <your tests> -q` and
  `python -m ruff check <your dirs>` must pass before you report.
  Desktop: `cd app && npx vitest run <your tests>` must pass.

---

## Contract 1 — `GET /integrations` (service)

One route, aggregated, read-only, no secrets, always 200. `Cache-Control:
no-store`. Owned by **P1**; P2/P3 code against this shape and must not change
it.

```jsonc
{
  "schema_version": 1,
  "integrations": [
    {
      "id": "slack",              // stable slug, see the roster below
      "name": "Slack",
      "kind": "delivery",         // delivery | meeting | design | fabrication
      "state": "ready",           // ready | partial | unconfigured | unavailable
      "summary": "Posts runs into a thread in #hardware.",
      "detail": "Signing secret set; bot token set; 2 channels allowlisted.",
      "settings": [               // never a value, only whether it is set
        {"key": "SLACK_BOT_TOKEN", "required": true, "set": true,
         "shown": "xoxb-…4f2a", "note": ""}
      ],
      "hints": ["Set SLACK_SIGNING_SECRET in the service's environment (the service does not read .env)."],
      "actions": [                // things the panel may offer; [] is fine
        {"id": "google_sign_in", "label": "Sign in with Google",
         "method": "POST", "path": "/deliver/auth/start"}
      ],
      "docs": "docs/slack.md",
      "unverified": false         // true = never run against a live account
    }
  ]
}
```

State meanings, and they are load-bearing:

- `unavailable` — the code is not importable here (optional extra missing,
  package absent). `hints` names the extra.
- `unconfigured` — importable, nothing set.
- `partial` — some required settings set, some missing. `hints` names each
  missing env var **and** says the service does not read `.env` (reuse
  `service/deliver.py`'s `_ENV_NOTE`).
- `ready` — everything required is present. This is a claim about
  configuration only, never about a live call succeeding; `detail` must not
  imply a successful round trip that did not happen.

Roster (ids are frozen): `google` (Workspace delivery), `slack`, `meet`,
`zoom`, `teams`, `cad` (enclosure kernel), `sourcing`, `mcp`, `spice`,
`kicad`. An id whose feature is not built yet still appears, with
`state: "unavailable"` and a hint saying it is not built — unbuilt work stays
visibly unbuilt.

`google` must be derived from the same source as `GET /deliver/config`
(`service/deliver.py:config_report`) so the two can never disagree; call it,
do not re-implement it.

## Contract 2 — desktop client module (`app/src/lib/silkscreen/integrations.ts`)

Owned by **P2**. Frozen exports:

```ts
export type IntegrationState = "ready" | "partial" | "unconfigured" | "unavailable";
export type IntegrationKind = "delivery" | "meeting" | "design" | "fabrication";
export interface IntegrationSetting { key: string; required: boolean; set: boolean; shown: string; note: string }
export interface IntegrationAction { id: string; label: string; method: "GET" | "POST"; path: string }
export interface Integration {
  id: string; name: string; kind: IntegrationKind; state: IntegrationState;
  summary: string; detail: string; settings: IntegrationSetting[];
  hints: string[]; actions: IntegrationAction[]; docs: string; unverified: boolean;
}
/** GET /integrations. Throws SilkscreenError on a bad response, never returns a partial list. */
export function fetchIntegrations(baseUrl: string, token: string, signal?: AbortSignal): Promise<Integration[]>;
/** Runs an action from an Integration.actions entry. */
export function runIntegrationAction(baseUrl: string, token: string, action: IntegrationAction): Promise<unknown>;
/** Pure: group by kind, stable order — delivery, meeting, design, fabrication. */
export function groupByKind(list: readonly Integration[]): { kind: IntegrationKind; items: Integration[] }[];
/** Pure: one-line status word + tone for a card badge. */
export function badgeFor(item: Integration): { label: string; tone: "ok" | "warn" | "off" };
```

Validation is the client's job: a response missing `integrations`, or an entry
with an unknown `state`, is a `SilkscreenError` — not a silently-empty page.

## Contract 3 — `zoombot/` (Zoom, live join-and-speak)

Package layout, frozen names:

```
zoombot/
  __init__.py     exports Config, ConfigError, load_config, Speaker, ...
  config.py       ZOOM_* env, validated at construction
  rtms.py         webhook verification + RTMS media/transcript ingest (Transport seam)
  speak.py        Speaker Protocol: MeetingSdkSpeaker | ChatSpeaker | NullSpeaker
  agent.py        transcript -> board requests (reuses meetings.intent gates)
  runner.py       the only module that knows both Zoom and silkscreen
  app.py          HTTP surface: POST /zoom/events, GET /healthz
  __main__.py     python -m zoombot
  bot/            headless Meeting SDK container (Dockerfile + entrypoint + README)
  tests/
```

Basis, and cite it in the module docstrings: Zoom's own open-source
`zoom/rtms` samples for Realtime Media Streams (webhook `endpoint.url_validation`
HMAC handshake, then a signalling WebSocket, then a media WebSocket), and
`zoom/meetingsdk-headless-linux-sample` for the participant that can actually
emit audio. Prefer the officially-maintained upstream shape over anything
clever.

The honest boundary, which must appear in the docstring and the docs page:
**RTMS is receive-only.** Audio out requires a real participant, which is the
headless Meeting SDK container in `bot/`. That container is not built or run by
the test suite and has never been run against a live Zoom account here.
`Speaker` is the seam that makes this statable: `MeetingSdkSpeaker` talks to
the container, `ChatSpeaker` posts into the meeting chat over the Zoom REST
API, `NullSpeaker` records what would have been said. `runner.py` names which
speaker it used in its report, so "the agent replied" can never be read as
"the agent spoke out loud" when it did not.

Frozen signatures the other Zoom agents compile against:

```python
# config.py
ZOOM_ENV = ("ZOOM_CLIENT_ID","ZOOM_CLIENT_SECRET","ZOOM_ACCOUNT_ID",
            "ZOOM_WEBHOOK_SECRET_TOKEN","ZOOM_RTMS_ENABLED","ZOOM_SPEAK_MODE",
            "ZOOM_MEETINGS","ZOOM_MAX_RUNS_PER_MEETING","ZOOM_API_BASE")
@dataclass(frozen=True)
class Config:
    client_id: str; client_secret: str; account_id: str
    webhook_secret: str; api_base: str = "https://api.zoom.us/v2"
    speak_mode: str = "chat"          # "sdk" | "chat" | "off"
    meeting_allowlist: tuple[str, ...] = ()
    max_runs_per_meeting: int = 2
    def redacted(self) -> dict[str, str]: ...
    def allows(self, meeting_id: str) -> bool: ...
def load_config(env: Mapping[str, str] | None = None) -> Config: ...  # ConfigError names every gap at once

# rtms.py
class Transport(Protocol):
    def get(self, url: str, headers: Mapping[str, str]) -> bytes: ...
    def post(self, url: str, headers: Mapping[str, str], body: bytes) -> bytes: ...
class MediaStream(Protocol):
    def __iter__(self) -> Iterator[TranscriptChunk]: ...
@dataclass(frozen=True)
class TranscriptChunk: meeting_id: str; speaker: str; text: str; at_ms: int
def verify_webhook(config: Config, headers, body: bytes, *, now=None) -> dict   # raises WebhookError
def url_validation_reply(config: Config, payload: dict) -> dict                  # the plainToken/encryptedToken handshake
def open_stream(config, payload: dict, *, connect=None) -> MediaStream

# speak.py
class Speaker(Protocol):
    name: str
    def say(self, meeting_id: str, text: str) -> None: ...
def speaker_for(config: Config, *, transport=None) -> Speaker

# agent.py
def requests_from_chunks(model, chunks: Iterable[TranscriptChunk], *, max_requests: int = 3) -> list[BoardRequest]
# runner.py
@dataclass class ZoomReport: meeting_id: str; considered: list; runs: list; spoke_via: str; warnings: list[str]
def run_meeting(config, model, stream: MediaStream, *, speaker=None, generate=None, **kw) -> ZoomReport
```

Gates carried over from `meetings/runner.py` and non-negotiable: a request
whose quote is not in the transcript is dropped; a request below
`DEFAULT_CONFIDENCE_FLOOR` is recorded in `considered` but not built; ongoing
work never orders anything; `max_runs_per_meeting` caps paid runs; a webhook
replay older than five minutes is refused and event ids are remembered
(`slackbot/app.py` is the model for all of this — verify the signature
**before** parsing the body).

## Contract 4 — `teamsbot/` (Microsoft Teams, same depth)

Mirror `zoombot/` file for file: `config.py`, `graph.py` (transport +
allowlist, exact-host match on `graph.microsoft.com` / `*.communication.azure.com`),
`speak.py`, `agent.py`, `runner.py`, `app.py`, `__main__.py`, `bot/`, `tests/`.

Basis to cite: `microsoft/BotBuilder-Samples` (calling-bot shape, the
`/api/calls` webhook and JWT validation) and the Graph
`communications/calls` + `callTranscripts` REST surface. Live audio in and out
is a policy-gated Graph calling bot with a media platform; that half is
`bot/` and is unverified here, same statement as Zoom's.

Env names frozen: `TEAMS_APP_ID`, `TEAMS_APP_SECRET`, `TEAMS_TENANT_ID`,
`TEAMS_BOT_ENDPOINT`, `TEAMS_SPEAK_MODE` (`sdk|chat|off`), `TEAMS_MEETINGS`,
`TEAMS_MAX_RUNS_PER_MEETING`, `TEAMS_GRAPH_BASE`.

`ChatSpeaker` posts to the meeting chat via
`/chats/{id}/messages`; that path is reachable with application permissions
and is the one half that could plausibly be exercised live first.

## Contract 5 — structured spec review

The product idea: instead of returning a wall of text after a run, Kaleo books
time and shows up. The review's blockers become an agenda, the agenda becomes
a calendar hold with a Meet link, and the humans decide the open questions in
the meeting.

**S1 — the IR** (`engine/silkscreen/specreview.py`, tests in `engine/tests/`):
`SpecReview` is validated model output, `netlist.py` conventions throughout
(fenced JSON tolerated, every failure batched into one
`SpecReviewValidationError`).

```python
@dataclass(frozen=True)
class AgendaItem:
    topic: str; why: str; minutes: int; blocking: bool
    refs: tuple[str, ...]          # part refs / net names the item is about
@dataclass(frozen=True)
class SpecReview:
    title: str; summary: str        # <= 400 chars, the anti-wall-of-text rule
    items: tuple[AgendaItem, ...]   # 1..8, total minutes 15..60
    decisions_needed: tuple[str, ...]
    prepared_from: tuple[str, ...]  # "review", "route", "kernel", "sourcing"
    def total_minutes(self) -> int: ...
    def as_dict(self) -> dict: ...
def parse_spec_review(raw: str) -> SpecReview: ...
```

An item whose `refs` name nothing the board contains is dropped, not reported
— the `review.py` hallucination filter, applied again. A `SpecReview` with no
blocking item is a legitimate result and must be reportable as "nothing needs
a meeting"; do not invent an agenda to fill the slot.
`agents/specreview.py` holds the model call and the one repair round, shaped
exactly like `agents/sourcing.py`, with a `SPECREVIEW_MARKER` for
`ScriptedModel.by_marker`, and gives up loudly rather than half-parsing.

**S2 — scheduling** (`googleapps/specreview.py`, `googleapps/calendar.py`
edits, `service/deliver.py` edits, `googleapps/__main__.py` flag):
a new deliver destination `spec_review`. Request gains
`{"spec_review": true, "attendees": [...], "when": "<RFC3339>" | null}`;
response gains a `spec_review` block in the same per-destination shape as
`calendar` (`{"ok", "html_link"?, "meet_uri"?, "skipped_reason"?, "error"?}`).
Keep every existing rule: `sendUpdates=all`, addresses validated before any
network call, the blockers-only honesty (if the review has not run, say "the
review step has not run" — do not guess), the agenda in the event description
HTML-escaped. CLI flag `--spec-review` alongside `--schedule`.
**You own `service/deliver.py` and `googleapps/`. You do not touch
`service/app.py`.**

**S3 — the desktop half** (`app/src/pages/kaleo/components/DeliverPanel.tsx`,
`RunOptions.tsx`, `app/src/lib/silkscreen/types.ts`, `deliver.ts`, and their
tests): a "Book a spec review" destination in the deliver panel showing the
agenda and total minutes before it sends anything, and a
**Structured / Prose** output toggle in `RunOptions` that chooses whether a
finished run is summarised as a structured agenda or as prose. One request in
flight at a time — the existing `inFlightRef` rule, because every button is a
real send. **You own `types.ts`; nobody else edits it.**

## File ownership (a conflict here costs more than the work)

| Agent | Owns, exclusively |
|---|---|
| P1 | `service/integrations.py`, `service/tests/test_integrations.py`, the new arm in `service/app.py` |
| P2 | `app/src/lib/silkscreen/integrations.ts` + its tests |
| P3 | `app/src/pages/integrations/**`, `app/src/pages/index.ts`, `app/src/routes/index.tsx`, `app/src/hooks/useMenuItems.tsx` |
| Z1 | `zoombot/config.py`, `zoombot/__init__.py`, `zoombot/tests/test_config.py` |
| Z2 | `zoombot/rtms.py`, `zoombot/tests/test_rtms.py` |
| Z3 | `zoombot/speak.py`, `zoombot/bot/**`, `zoombot/tests/test_speak.py` |
| Z4 | `zoombot/agent.py`, `runner.py`, `app.py`, `__main__.py`, `zoombot/tests/test_agent.py`, `test_runner.py`, `test_http.py` |
| T1 | `teamsbot/config.py`, `graph.py`, `__init__.py`, their tests |
| T2 | `teamsbot/agent.py`, `runner.py`, their tests |
| T3 | `teamsbot/speak.py`, `app.py`, `__main__.py`, `bot/**`, their tests |
| S1 | `engine/silkscreen/specreview.py`, `engine/silkscreen/agents/specreview.py`, `engine/tests/test_specreview.py` |
| S2 | `googleapps/**`, `service/deliver.py` |
| S3 | `app/src/pages/kaleo/components/DeliverPanel*.tsx`, `RunOptions.tsx`, `app/src/lib/silkscreen/types.ts`, `deliver.ts` |

Test-file basenames must be globally unique across packages —
`scripts/check_docs.py` keys per-module counts by basename, which is why
`slackbot/tests/test_http.py` is not `test_app.py`. Prefix yours if it would
collide (`zoombot/tests/test_zoom_config.py`, not `test_config.py`).
