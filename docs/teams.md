# Microsoft Teams front end (`teamsbot/`)

`python -m teamsbot` puts the pipeline in a Teams meeting. Two ways in: a
message addressed to the bot in a meeting chat (`POST /api/messages`, the Bot
Framework activity webhook), and the Graph calling webhook (`POST /api/calls`)
for a meeting the bot was invited into. The post-hoc path reads a finished
meeting's `callTranscript` through Microsoft Graph. What comes back is a
drafted board and a message saying what it did and did not build.

Structured like `service/` and `slackbot/` — standard library only (no
`requests`, no `msal`, no `botbuilder`), no engine logic of its own, and
`teamsbot/runner.py` is the only module that knows both Teams and silkscreen.
The shape is cited rather than invented:
[`microsoft/BotBuilder-Samples`](https://github.com/microsoft/BotBuilder-Samples)
for the calling-bot shape (an Entra app with a public `/api/calls` endpoint
that validates the inbound token) and the documented Graph REST surface for
online meetings and call transcripts.

---

## Unverified — read this before the feature

1. **Nothing here has ever run against a live Microsoft 365 tenant.** Not one
   notification, not one Graph call, not one chat message. Every network
   boundary is a Protocol seam (`graph.Transport`, `speak.BotTransport`,
   `app.KeySource`) with a recorded stand-in, so the suite runs offline with no
   credentials. That proves the parsing, the gates and the refusals — the JWT
   checks are exercised with a locally generated key pair — and proves nothing
   about Microsoft's live behaviour.

2. **The media-out half is a specification plus a refusing stub.** Speaking out
   loud in a Teams meeting needs a real participant with a media session: a
   policy-gated Graph *calling bot* using Microsoft's Graph Communications
   calling and media libraries (`Microsoft.Graph.Communications.Calls.Media`),
   which are .NET components with their own platform requirements and
   licensing. Those are **not vendored here**. `teamsbot/bot/` holds only the
   control plane — `control.py` (`GET /healthz`, `POST /say`) and a Dockerfile
   — and `POST /say` answers **501 Not Implemented** with a sentence naming
   what is missing. That surfaces through `CallingBotSpeaker` as
   `SpeakError("bot_refused", …)` and lands in the report a person reads. The
   alternatives — answer 200, answer nothing, ship a plausible media stub —
   were all rejected: a run must never claim the agent spoke in a meeting where
   no audio existed.

3. **`ChatSpeaker` is also unverified**, though it is the half most likely to
   work first: one application permission and one `POST /chats/{id}/messages`.

4. **Auth constants checked against Microsoft's published documents and SDK
   source on 2026-09-08; the Graph surface still unchecked.** The three
   constants that decide whether an inbound token is believed were verified
   without a tenant — the OpenID metadata documents are public and fetchable,
   and the botbuilder SDKs are open source. That found one real error. The
   Graph *request* surface below could not be verified this way and stays
   documentation-derived.

   | Constant / assumption | Verdict | Evidence |
   |---|---|---|
   | `ACCEPTED_ISSUERS = ("https://api.botframework.com",)` (activity path) | **CONFIRMED, twice** | `microsoft/botbuilder-python` `authentication_constants.py`: `TO_BOT_FROM_CHANNEL_TOKEN_ISSUER = "https://api.botframework.com"`; `microsoft/botbuilder-dotnet` `AuthenticationConstants.cs`: `ToBotFromChannelTokenIssuer`. **And** both metadata documents below were fetched and each declares `"issuer": "https://api.botframework.com"`. |
   | The same tuple used for `POST /api/calls` | **WRONG, fixed** | Microsoft's Graph calling sample (`microsoftgraph/microsoft-graph-comms-samples`, `Samples/Common/Sample.Common/Authentication/AuthenticationProvider.cs`) validates a calling notification against **two** issuers: `https://graph.microsoft.com` *and* `https://api.botframework.com`. A graph-issued notification was refused `bad_issuer` — closed and noisy, the right direction to be wrong in, but `/api/calls` could never have worked. Now a separate `CALLING_ISSUERS`, deliberately **not** merged into the activity list. Weakest citation in the file: that sample carries Microsoft's own "HAS NOT BEEN TESTED RIGOROUSLY … PURELY FOR DEMONSTRATION PURPOSES" banner. |
   | `OPENID_CONFIGURATION_URLS` | **CONFIRMED by fetching them** | `login.botframework.com/v1/.well-known/openidconfiguration` → issuer `https://api.botframework.com`, `jwks_uri` `https://login.botframework.com/v1/.well-known/keys`. `api.aps.skype.com/v1/.well-known/openidconfiguration` → same issuer, `jwks_uri` `https://api.aps.skype.com/v1/keys`. The first matches `ToBotFromChannelOpenIdMetadataUrl` in both SDKs; the second matches the `authDomain` constant in the Graph calling sample. |
   | `KEY_HOSTS` | **CONFIRMED for two of three** | `login.botframework.com` and `api.aps.skype.com` are the hosts of the `jwks_uri` values actually published above. `login.microsoftonline.com` is unused today — it covers the emulator/skill metadata URL the SDKs name — an allowlist entry with nothing behind it, kept deliberately. |
   | Algorithms `RS256`/`RS384`/`RS512` only | **CONFIRMED** | Both SDKs declare `ALLOWED_SIGNING_ALGORITHMS = ["RS256", "RS384", "RS512"]` and never supply a symmetric key. Matches this repo independently. (Curiosity: the skype document advertises `"RSA256"`, not a real JWA name — presumably Microsoft's typo. Nothing here reads that field, so it is inert.) |
   | `CLOCK_SKEW_S = 300` | **CONFIRMED** | Five minutes in both SDKs (`ClockSkew = TimeSpan.FromMinutes(5)` / `clock_tolerance=5*60`). |
   | `MAX_TOKEN_AGE_S = 3600` | **NOT UPSTREAM — this repo's own choice** | Neither SDK enforces an `iat` ceiling at all; they rely on `exp`/`nbf` plus skew. Ours is strictly *stricter* than Microsoft's, so it can refuse a token Microsoft would accept but never the reverse. If a live run shows legitimate tokens older than an hour, this is the line to raise. |
   | Fail-closed on a missing key source or crypto backend | **CONFIRMED as the upstream posture** | Python's extractor raises on any metadata/JWKS fetch failure; .NET's only fallback is reusing a *previously cached* config, against which the token is still cryptographically verified. Neither ever accepts an unverified token — matching `no_key_source` / `crypto_unavailable` here. |
   | Port `3978` | `config.DEFAULT_PORT` | The Bot Framework convention every sample and ngrok recipe uses. Only the default: set `TEAMS_PORT`, or pass `port=` to `make_server`. |
   | Meeting chat id form `19:meeting_…@thread.v2` | **UNVERIFIABLE without a tenant** | No public schema states it and none was found. Deliberately **not** validated by a pattern: a regex written from a guess would refuse valid ids, and the string is not a security boundary — `message_url` puts the finished URL through `ensure_graph_url`, so a malformed id can only produce a wrong path on an allowlisted host. A wrong id is a Graph 404 → `TeamsError` → `SpeakError("graph_error", …)` carrying Graph's own message, with `spoke_via` left at `"none"`. |
   | Graph paths `users/{id}/onlineMeetings`, `…/transcripts`, `…/transcripts/{id}/content?$format=text/vtt` | **UNVERIFIABLE without a tenant** | The documented REST surface, never exercised live. A wrong path is a 404 with Graph's message attached, never an empty transcript: the four transcript outcomes are four distinct exception types. |
   | `allowTranscription` on an `onlineMeeting` | `graph._meeting` | Read tri-state: `True`/`False` are what the tenant said, `None` means the field was absent — "we do not know" is not "transcription is off". |
   | WebVTT `<v Speaker Name>text</v>` cues | `graph.vtt_to_lines` | How Teams writes the speaker into a cue. A cue with no voice tag keeps its text under `unknown` rather than vanishing. |
   | `$filter=JoinWebUrl eq '…'` | `graph.meetings` | The documented meeting lookup. |

   One further boundary, which is Graph's behaviour rather than an assumption:
   the transcript `content` route can legitimately answer with a **redirect to
   blob storage**, and this package refuses every redirect (a 3xx would carry
   the bearer token off the allowlist). Such a response surfaces as
   `TeamsRefusedError` naming the redirect — not as a silently empty
   transcript. Whether the live route actually redirects is one of the things a
   first live run will settle.

None of this is "should work". It is built, tested offline, and unproven.

---

## Environment

Every variable is listed in `teamsbot.config.TEAMS_ENV`, validated once in
`load_config`, and named in the error when it is missing — every gap at once.
`Config.redacted()` prints `<set, N chars>`, never a value and never a tail
(`app_id` and `tenant_id` are masked too: a redaction rule with exceptions is a
rule someone gets wrong later).

| Variable | Required | Default | What it is |
|---|---|---|---|
| `TEAMS_APP_ID` | yes | — | Entra ID app registration client id. Also the `aud` an inbound JWT must carry |
| `TEAMS_APP_SECRET` | yes | — | The registration's client secret, for the client-credentials grant |
| `TEAMS_TENANT_ID` | yes | — | Directory the app is installed in; forms the token URL |
| `TEAMS_BOT_ENDPOINT` | conditional | empty | The public https callback URL Teams POSTs to. Optional in general, **required when `TEAMS_SPEAK_MODE=sdk`** — a calling bot with nowhere to be called is not a configuration this package pretends works |
| `TEAMS_SPEAK_MODE` | no | `chat` | `sdk` \| `chat` \| `off`. No fourth value, no silent fallback |
| `TEAMS_MEETINGS` | no | empty | Comma-separated meeting-id allowlist. **Empty means every meeting these credentials can see**, which with application permissions is the whole tenant |
| `TEAMS_MAX_RUNS_PER_MEETING` | no | `2` | Hard cap on *paid* pipeline runs from one meeting. Zero or negative is an error, not "unlimited" |
| `TEAMS_GRAPH_BASE` | no | `https://graph.microsoft.com/v1.0` | Must be https and end in `/v<N>` or `/beta`. `/beta` is deliberately not the default — it is documented as changing without notice |
| `TEAMS_PORT` | no | `3978` | Port `python -m teamsbot` binds, validated 1–65535. The callback URL registered with Microsoft names a port, so this is deployment configuration, not an implementation detail |

The service does **not** read `.env`. Export these into the shell that starts
the process.

---

## Registering the app

None of this can be arranged from inside the repository, and none of it has
been done here.

1. **An Entra ID (Azure AD) app registration** — its client id and secret are
   `TEAMS_APP_ID` / `TEAMS_APP_SECRET`, its directory `TEAMS_TENANT_ID`.

2. **Application permissions, admin-consented.** For the transcript half this
   package already uses:
   - `OnlineMeetings.Read.All`
   - `OnlineMeetingTranscript.Read.All`

   For the calling bot (the unbuilt half), additionally:
   - `Calls.JoinGroupCall.All` — join a meeting
   - `Calls.AccessMedia.All` — hear and speak. A tenant-wide capability, and
     the reason an administrator will ask questions.

   The client-credentials grant asks for the scope
   `https://graph.microsoft.com/.default`, which means "every application
   permission already consented for this registration" — the only scope form
   that grant accepts.

3. **A Teams application access policy**, granted with PowerShell
   (`New-CsApplicationAccessPolicy` / `Grant-CsApplicationAccessPolicy`) for
   the users whose meetings the bot may act in. Without it Graph answers **403**
   for a meeting the registration otherwise has permission for — a failure that
   reads as a bug and is a policy. `graph._explain` says so in the 403 message.

4. **A publicly reachable https callback endpoint** — `TEAMS_BOT_ENDPOINT`,
   pointing at `/api/calls` (and `/api/messages`). A tunnel (ngrok, dev
   tunnels) is the usual development answer; the certificate must be real.

5. **A Bot Framework / Azure Bot resource** registered against the same app id,
   with the Teams channel and calling enabled, pointed at the same endpoint.

6. **Media ports and a media platform certificate** for the calling-bot
   container, per Microsoft's calling-bot deployment documentation — the part
   that is genuinely awkward to run outside Azure.

Steps 1–2 (transcript permissions) and 3 are enough for everything this
repository actually implements. Steps 4–6 belong to the unbuilt half.

---

## Running it

```bash
pip install -e ".[dev,agents,teams]"    # the teams extra adds cryptography
export TEAMS_APP_ID=… TEAMS_APP_SECRET=… TEAMS_TENANT_ID=… GOOGLE_API_KEY=…
python -m teamsbot                      # binds TEAMS_PORT (default 3978)
```

| Method | Path | What it does |
|---|---|---|
| `POST` | `/api/calls` | Graph calling notifications (call lifecycle) |
| `POST` | `/api/messages` | Bot Framework activities — where a typed request arrives |
| `GET` | `/healthz` | `{"ok": true, "service": "silkscreen-teams"}` |

Exit code 2 means the configuration is bad; the message names every gap.

`cryptography` is an extra rather than a silent optional because RS256 is not
something the standard library can verify. Without it the endpoint **refuses
every request** (`crypto_unavailable`) rather than accepting one unverified —
an endpoint that waves a token through because it could not check it is worse
than one that is switched off, since it looks like it is working. A missing
`KeySource` is the same kind of refusal (`no_key_source`).

### What happens on a delivery

1. **Authenticity before parsing.** `verify_notification` checks, in order:
   the JOSE header names a real asymmetric algorithm (`RS256`/`RS384`/`RS512`
   — never `none`, never an HMAC, the classic JWT forgery); `iss` is in
   `ACCEPTED_ISSUERS`; `aud` contains **this** bot's `TEAMS_APP_ID`, so a token
   minted for someone else's bot is refused; `exp`/`nbf`/`iat` are inside the
   window with `CLOCK_SKEW_S` of slack and no older than `MAX_TOKEN_AGE_S`; and
   finally the signature verifies against the key the `kid` names. There is no
   path through that function that returns without a verified signature. The
   reason never reaches the client — the response is a bare 401.
2. **Notification ids are remembered.** Teams retries what it does not see
   acknowledged quickly, and a retry that slips through is a second *paid*
   pipeline run. A notification carrying no id gets one derived from its own
   content, because "no id" must not mean "always new".
3. **Acknowledge fast, work on a thread.** The 202 promises the request was
   accepted, never that a board exists. One concurrent run
   (`MAX_CONCURRENT_RUNS = 1`); a queued run waits `SLOT_WAIT_S` (240 s) before
   the meeting is told the bot is busy.
4. A `call` notification carries no text — call lifecycle is not a request — so
   it is logged and dropped rather than run. `runner.run_transcript` is what
   turns a finished call into requests.

Mentions are stripped from an activity's text before it becomes a prompt
(`<at>Kaleo</at>` and the display name go together), and the bot's own messages
are dropped by id, because two bots in one meeting chat would otherwise answer
each other indefinitely.

### The gates

- A request whose quote is **not literally in the transcript** is dropped.
  `teamsbot/agent.py` owns no extraction logic of its own — it calls
  `meetings.intent.extract_requests`, which batches every validation failure
  into one error and applies the hallucination filter. A second implementation
  would be a second place for it to drift.
- A request below `DEFAULT_CONFIDENCE_FLOOR` (0.6) is recorded in
  `TeamsReport.considered` and **not built**.
- `max_runs_per_meeting` caps paid runs, and capped requests are reported as
  capped.
- **A meeting still in progress is skipped entirely.** Half a meeting is half a
  requirement, and the other half often contradicts the first.
- **Nothing is ever ordered.** There is no ordering path in this package at
  all; `runner.NO_ORDER_NOTE` says so out loud when someone asks.

A calling bot delivers speech in fragments, so `teamsbot/agent.py` gathers them
into windows first, under a named `WindowPolicy` (overlap so a request split
across a boundary stays quotable; a long silence closes a window early; and
identical quotes are collapsed afterwards, keeping the higher confidence, so
overlap does not double the bill). An overlap not strictly smaller than the
span is a construction-time error rather than a hang.

Four Graph outcomes stay four exception types rather than one empty string,
because an agent handed `""` concludes the meeting was silent:
`TranscriptionDisabledError` (never enabled — retrying is pointless, the fix is
a meeting setting), `NoTranscriptError` (none recorded, still processing, or
empty), `TeamsAuthError` (401/403 — usually the missing application access
policy), and `TeamsCallError` (anything else).

---

## Speaking, and saying which way it went

| Mode | Speaker | `name` | Audible in the meeting? |
|---|---|---|---|
| `sdk` | `CallingBotSpeaker` | `calling-bot` | Yes — and only with a real, implemented container |
| `chat` | `ChatSpeaker` | `chat` | No. `POST /chats/{id}/messages`, `contentType: text` |
| `off` | `NullSpeaker` | `null` | No. Records `Utterance`s in `said` |

`TeamsReport.spoke_via` prints the name of the speaker that actually delivered
the message; a speaker that raised leaves it at `"none"` and puts the failure in
`warnings`. A claim that something was said is only ever made when it was. The
`NullSpeaker` is named `"null"` rather than `"none"` precisely so "the operator
switched speaking off" and "delivery failed" cannot share a string.

Chat messages are sent as plain text, not HTML: Teams renders a subset of HTML
for `html` content, and a board summary is full of net names and
angle-bracketed values. Plain text cannot be mis-rendered and cannot inject
markup into somebody's chat. Anything over `MAX_MESSAGE_CHARS` (3800) is
**refused**, not truncated — a silently half-sent answer is worse than a stated
refusal — and reading four thousand words into a live call is a hazard, not a
feature.

`SpeakError.code` distinguishes `bot_unreachable`, `bot_refused`,
`redirect_refused`, `bad_host`, `graph_error`, `too_long`, `empty`,
`bad_speak_mode`.

### The `bot/` container

Loopback-only on `127.0.0.1:8791` (`speak.DEFAULT_CONTROL_URL`), unauthenticated
by design — it is a sidecar to the process holding the call, so
`speak.ensure_control_url` allows https anywhere but plaintext http **only** on
loopback. Anything that can reach that port can make the bot talk in a meeting.

```bash
docker build -t silkscreen-teams-callingbot teamsbot/bot
docker run --rm -p 127.0.0.1:8791:8791 silkscreen-teams-callingbot
curl -s http://127.0.0.1:8791/healthz
# {"ok": true, "service": "silkscreen-teams-callingbot", "can_speak": false}
curl -s -XPOST http://127.0.0.1:8791/say -H 'content-type: application/json' \
  -d '{"meeting_id":"19:meeting_x@thread.v2","text":"hello"}'
# 501, with the sentence naming the missing media stack
```

With the container stopped, saying something raises
`SpeakError("bot_unreachable", …)`; with it running,
`SpeakError("bot_refused", …)` carrying the 501's sentence. Neither is ever
silence, and neither is ever reported as having spoken. To make it real,
replace `say()` in `control.py` — that is the whole contract: given a meeting
id and a sentence, put that sentence into that call's audio, or raise.

---

## Security boundaries

- **Exact-host allowlist** — `graph.ALLOWED_HOSTS` is `graph.microsoft.com` and
  `login.microsoftonline.com` only. A suffix check would wave through
  `graph.microsoft.com.evil.example`. The Azure Communication Services host is
  deliberately absent: nothing here calls it, and an allowlist entry for an
  unused host is an open door.
- `ensure_graph_url` runs at **request-construction** time, so it holds for
  every transport including the fakes — building a request for an unknown host
  is wrong whether or not it would have left the machine. An `@odata.nextLink`
  or a `transcriptContentUrl` from a response goes through the same check: data
  from the network does not get to choose which host receives the bearer token.
- **Redirects are refused** everywhere, on Graph and on the control surface.
- The app token is **cached in memory only, never written to disk**, and renewed
  `TOKEN_SKEW_S` (60 s) early; a missing or unreadable `expires_in` is treated
  as "expires now-ish" rather than "lasts forever".
- **Bounded**: 1 MiB request bodies (drained at most 2 MiB, with a 30 s
  deadline, before authentication), 8 MiB Graph responses, 50 pages of
  `@odata.nextLink`, a 15 s socket timeout.
- Run memory is **in-process only**; a follow-up after a restart gets
  `MISSING_RUN_NOTE` rather than an answer about whichever board is lying
  around. `graph.meetings` deliberately claims **no ordering** — no `$orderby`
  is sent and nothing is sorted, so taking a cap off the front would pick
  arbitrary meetings.

One difference from Meet worth stating: Meet's transcript entries carry opaque
participant ids, while Teams' WebVTT carries the speaker's **display name**
inline. There is no id form in the content, so the display name is what comes
out. That is a real difference in what this integration sees, not a choice made
here.

---

## Tests

`teamsbot/tests/` — `test_teams_config.py`, `test_teams_graph.py`,
`test_teams_speak.py`, `test_teams_agent.py`, `test_teams_runner.py`,
`test_teams_http.py`. All offline: a recorded transport for Graph and for the
control surface, a locally generated RSA key pair for the JWT checks, and
`ScriptedModel` for the extraction. They are in `testpaths`, so
`python -m pytest -q` runs them, and `ruff check … zoombot teamsbot` lints
them. The basenames carry a `teams_` prefix because `scripts/check_docs.py`
keys per-module counts by basename.

Nothing in the suite builds the container, starts it, or reaches Microsoft.
