# Zoom front end (`zoombot/`)

`python -m zoombot` puts the pipeline in a Zoom meeting: it receives the
meeting's live transcript over Zoom's **Realtime Media Streams** (RTMS), pulls
board requests out of what was actually said, runs `generate_pcb`, and answers
in the room.

Structured like `service/` and `slackbot/` — standard library only, no engine
logic of its own, and `zoombot/runner.py` is the only module that knows both
Zoom and silkscreen. The shape follows Zoom's own open-source samples:
[`zoom/rtms`](https://github.com/zoom/rtms) for the webhook handshake and the
signalling-then-media WebSocket sequence, and
[`zoom/meetingsdk-headless-linux-sample`](https://github.com/zoom/meetingsdk-headless-linux-sample)
for the participant that can emit audio.

---

## Unverified — read this before the feature

This is not a footnote. Four separate things are unproven, and the tests cannot
prove any of them:

1. **Nothing here has ever run against a live Zoom account.** Not one webhook,
   not one stream, not one chat message. Every network boundary is a Protocol
   seam (`rtms.Transport`, `rtms.Connection`, `speak.Transport`) with a
   recorded stand-in, so the whole suite passes offline with no credentials.
   That proves the parsing, the gates and the refusals. It proves nothing about
   Zoom's live behaviour.

2. **The media-out half is a specification plus a refusing stub.** RTMS is
   receive-only: it hands you a meeting's audio and transcript, and there is no
   REST call, webhook reply or RTMS message that makes a sound in a meeting.
   Speaking out loud requires being a participant, which is the headless
   Meeting SDK container in `zoombot/bot/`. That container is **not built and
   not run by the test suite**; its `entrypoint.sh` starts
   `zoombot/bot/control_stub.py`, which answers every `POST /say` with
   `{"spoken": false, "error": "the Zoom Meeting SDK participant is not
   implemented in this repository; ..."}`. The refusal is deliberate — a stub
   answering `{"spoken": true}` would let a run report `spoke_via:
   meeting_sdk` for a meeting the room heard nothing in. The Zoom Meeting SDK
   binary is downloaded under Zoom's own licence and is **not vendored here**;
   the `Dockerfile` expects it at `zoombot/bot/vendor/zoom-meeting-sdk-linux.tar.xz`
   and fails with that sentence when it is missing.

3. **`ChatSpeaker` is also unverified**, though it is the half most likely to
   work first: an ordinary Server-to-Server OAuth token and one POST, no
   container and no media plumbing.

4. **The RTMS protocol constants were checked against Zoom's own source on
   2026-09-08 — and several were wrong.** They used to be documented
   assumptions gathered in one block so a first live run could correct them.
   Instead they were checked, one at a time, against
   [`github.com/zoom/rtms-samples`](https://github.com/zoom/rtms-samples) (MIT,
   Zoom's maintained reference implementation), which needs no credential to
   read. Each constant in `zoombot/rtms.py` now cites the file and function it
   came from.

   | Constant / assumption | Verdict | Evidence |
   |---|---|---|
   | `MSG_TYPE` 1–4 and 12–18 | **CONFIRMED** | `signalingSocket.js` sends `msg_type: 1`; `mediaSocket.js` sends `msg_type: 3`; `signalingSocketMessageHandler.js` has `case 2`, `case 12` → replies `13`; `mediaSocketMessageHandler.js` has `case 4`, `case 17: // TRANSCRIPT`. |
   | `MSG_TYPE` 5–11 | **WRONG, fixed** | Was `STREAM_STATE_UPDATE = 5`, `EVENT_SUBSCRIPTION = 9`, `CLIENT_READY_ACK = 11`. The sample numbers them `EVENT_SUBSCRIPTION = 5` (`{ msg_type: 5, events: […] }`), `EVENT_UPDATE = 6` (`case 6: // Events`), `CLIENT_READY_ACK = 7` (`{ msg_type: 7, rtms_stream_id }`), `STREAM_STATE_UPDATE = 8` (`case 8: // Stream State changed`), `SESSION_STATE_UPDATE = 9` (`case 9`). A stream-state frame was being read as an event subscription. |
   | `MEDIA_TYPE_TRANSCRIPT = 8` | **CONFIRMED** | `mediaSocket.js`: `const TYPE_FLAGS = { audio: 1, video: 2, sharescreen: 4, transcript: 8, chat: 16 };` — and it is a **bitmask**, `1 << 3`, not an ordinal. |
   | `STATUS_OK = "STATUS_OK"` | **WRONG, fixed to the integer `0`** | Both handshake handlers gate on `if (msg.status_code === 0)`. There is no string status anywhere in the protocol. Against a real account **every handshake would have been read as a refusal**, and the run would have reported "never authorised" for a stream Zoom accepted. |
   | `TERMINAL_STREAM_STATES` (four strings) | **WRONG, fixed to `{4}`** | `rtmsEventLookupHelper.js::getRtmsStreamState` enumerates integers `0 INACTIVE … 4 TERMINATED … 6 RESUMED`, and the handler ends the stream on `if (msg.state === 4)`. `3 TERMINATING` is deliberately *not* terminal — the sample keeps reading — because ending there truncates a meeting. |
   | Session states | **ADDED** | A stopped session (`if (msg.state === 5 && conn)`, a *different* enum where `4` means RESUMED) also ends the stream. Without this branch a normally-ended session was reported as `StreamInterruptedError` — "half a meeting" said about a whole one. Kept as a separate constant so the two enums can never share a set. |
   | Handshake signature `"{client_id},{meeting_uuid},{rtms_stream_id}"` | **CONFIRMED, character for character** | `utils/signatureHelper.js`: ``const message = `${clientId},${rtmsId},${streamId}`;`` then HMAC-SHA256 hex under the client secret. |
   | Webhook signature `v0:<timestamp>:<body>` → `v0=<hex>` | **CONFIRMED** | `webhookManager/zoomWebhookSignature.js::verifyZoomWebhookRequest`, identical construction and identical `v0=` prefix. |
   | `MAX_WEBHOOK_AGE_S = 300` | **CONFIRMED** | Same file: `DEFAULT_WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS = 300`, applied two-sided with `Math.abs` as here. |
   | `endpoint.url_validation` reply | **CONFIRMED** | `buildUrlValidationResponse`: HMAC-SHA256 of the bare `plainToken` under the secret token, hex, returned as `{ plainToken, encryptedToken }`. Exactly what `url_validation_reply` does. |
   | `x-zm-request-timestamp` units | **CONFIRMED: seconds** | `verifyZoomWebhookRequest` computes `Math.abs(Math.floor(Date.now() / 1000) - timestampSeconds)`. Note the `event_ts` *inside* the body is milliseconds — two timestamps, two units, one delivery, which is why this was hedged. The millisecond fallback is kept but now **logs a warning**, so if the assumption is ever wrong a live run says so instead of silently never going stale. |
   | `payload.object.meeting_uuid` / `.rtms_stream_id` / `.server_urls` | **WRONG, fixed** | RTMS events are **flat on `payload`**, not nested under `.object`: `RTMSManager.js` destructures `const { meeting_uuid, rtms_stream_id, server_urls, event_ts } = payload;` with no `.object` step. Two consequences, both fatal: `session_from_payload` raised `PayloadError` on *every* real event, and `event_key` digested every event to the same key — so the first meeting was accepted and every later one refused as a replay. Both shapes are now read, since Zoom's older event families genuinely do nest. |
   | Media URL at `media_server.server_urls` | **CONFIRMED, order corrected** | `utils/rtmsEntityHelper.js::getPreferredMediaUrl` handles a bare string or an object keyed by media type, preferring **the requested type first**, then `all`, then `audio`, then any `ws`-prefixed value. We took `all` first; since we subscribe to transcript alone, that asked Zoom for audio and video we then dropped. |
   | Transcript frame fields `content.data`, `.user_name`, `.user_id`, `.timestamp` | **CONFIRMED** | `mediaSocketMessageHandler.js` `case 17` destructures `{ user_id, user_name, data, timestamp, start_time, end_time, language, attribute }`. |
   | `SESSION_STATE_REQ` / `_RESP` (10, 11) | **UNVERIFIABLE** | The two values the sample never sends or handles. Inferred from the gap between 9 and 12. Nothing in this module uses them, and an unknown `msg_type` is ignored by the frame loop — the right behaviour for a protocol that gains message types. |

   What none of this proves is that Zoom *behaves* as its own sample expects.
   The wire vocabulary is now right; whether a real account grants the scopes,
   opens the sockets and sends transcript frames at all is still unknown.

Do not read any of this as "should work". The constants are now checked
against Zoom's source rather than guessed, which removes one class of failure
and not the others: until someone has joined a real meeting and heard the
answer, points 1–3 stand unchanged.

---

## Environment

Every variable is listed in `zoombot.config.ZOOM_ENV`, validated once in
`load_config`, and named in the error when it is missing — every gap at once,
not one per restart. Secrets are never echoed: `Config.redacted()` prints
`<set, N chars>`, never a value and never a tail.

| Variable | Required | Default | What it is |
|---|---|---|---|
| `ZOOM_CLIENT_ID` | yes | — | Server-to-Server OAuth app client id; also the identity in the RTMS handshake signature |
| `ZOOM_CLIENT_SECRET` | yes | — | Server-to-Server OAuth client secret; signs the RTMS handshake |
| `ZOOM_ACCOUNT_ID` | yes | — | Zoom account id for the `account_credentials` grant |
| `ZOOM_WEBHOOK_SECRET_TOKEN` | yes | — | Webhook secret token; verifies `x-zm-signature` and answers the `endpoint.url_validation` challenge |
| `ZOOM_RTMS_ENABLED` | no | `true` | `1/true/yes/on` or `0/false/no/off`. Off means `open_stream` **refuses** rather than returning an empty stream |
| `ZOOM_SPEAK_MODE` | no | `chat` | `sdk` \| `chat` \| `off`. No fourth value, no silent fallback |
| `ZOOM_MEETINGS` | no | empty | Comma-separated meeting-id allowlist. **Empty means every meeting these credentials can see**, which on an account-level app is the whole account |
| `ZOOM_MAX_RUNS_PER_MEETING` | no | `2` | Hard cap on *paid* pipeline runs from one meeting. Zero or negative is an error, not "unlimited" |
| `ZOOM_API_BASE` | no | `https://api.zoom.us/v2` | Must be https and must end in `/v<N>`; an unpinned base lets a major-version change reshape responses silently |
| `ZOOM_PORT` | no | `8095` | Port `python -m zoombot` binds. Not 8080/8081 — both are taken on this team's machines |

Two more are read **only by the container** in `zoombot/bot/`, never by the
Python package: `ZOOM_SDK_KEY` and `ZOOM_SDK_SECRET` (the Meeting SDK app's
credentials, used to sign the join JWT), plus `ZOOM_MEETING_NUMBER` and the
optional `TTS_CMD` and `BOT_CONTROL_PORT`.

The service does **not** read `.env` — only `python -m silkscreen` and
`python -m googleapps` do. Export these into the shell that starts the process.

---

## Registering the Zoom app

Two different app types are needed, and they are not interchangeable.

**1. A Server-to-Server OAuth app** (Zoom App Marketplace → Develop → Build
App). Gives `ZOOM_CLIENT_ID`, `ZOOM_CLIENT_SECRET` and `ZOOM_ACCOUNT_ID`. Add
the webhook feature and copy its **Secret Token** into
`ZOOM_WEBHOOK_SECRET_TOKEN`.

Scopes, kept to what an admin reading the consent screen would accept:

- `meeting:read:meeting:admin` — resolve a meeting the bot is asked about.
- `meeting:write:chat_message:admin` — **only** for `ZOOM_SPEAK_MODE=chat`
  (`POST {api_base}/live_meetings/{meetingId}/chat/messages`).

**2. Realtime Media Streams** must be enabled for the app and the account, and
the app subscribed to the events this package handles:

- `endpoint.url_validation` — the HMAC handshake, answered inline.
- `meeting.rtms_started` — the notification that carries the signalling URL.

Every other event is acknowledged and ignored (`{"ok": true, "ignored": ...}`),
because there is nothing to do with one that would not be guessing.

Point the app's **Event notification endpoint URL** at
`https://<your host>/zoom/events`.

**3. A Meeting SDK app**, only if you want audio out. Its Client ID/Secret are
the container's `ZOOM_SDK_KEY` / `ZOOM_SDK_SECRET`. Meeting SDK apps carry no
OAuth scopes; the join is authorised by a JWT the container signs, plus the
passcode or a ZAK. To *send* audio the app needs raw-data / local-recording
permission, which is an account-level setting plus, in most accounts, the host
granting recording permission to the participant. Participants must be told a
bot has joined — do not disable the join chime, and check your jurisdiction.

---

## Running it

```bash
pip install -e ".[dev,agents,zoom]"     # the zoom extra adds websocket-client
export ZOOM_CLIENT_ID=… ZOOM_CLIENT_SECRET=… ZOOM_ACCOUNT_ID=…
export ZOOM_WEBHOOK_SECRET_TOKEN=… GOOGLE_API_KEY=…
python -m zoombot                       # binds ZOOM_PORT (default 8095)
```

Two routes, and nothing else:

| Method | Path | What it does |
|---|---|---|
| `POST` | `/zoom/events` | The webhook. Verifies, then dispatches. |
| `GET` | `/healthz` | `{"ok": true, "service": "silkscreen-zoom"}` |

Exit code 2 means the configuration is bad; the message names every gap.

`websocket-client` is imported **lazily**, inside `rtms.websocket_connect`, so
the package imports, tests, and reports its own configuration on a machine with
no WebSocket library at all — a missing package says so by name rather than
surfacing as a connection failure.

### What happens on a delivery

1. The HMAC is checked against the **raw bytes** before the body is parsed —
   `verify_webhook` is the only thing that produces a parsed payload, so no
   code path can act on an unverified body (`slackbot/app.py`'s rule).
2. A delivery older than 300 s is refused (`reason="stale"`), and deliveries
   are remembered by `x-zm-trackingid` (falling back to a digest of event,
   `event_ts`, `meeting_uuid` and `rtms_stream_id`). A replayed
   `meeting.rtms_started` would be a second *paid* pipeline run; a repeat is
   answered `200 {"ok": true, "duplicate": true}` and **not** re-run.
   `endpoint.url_validation` is exempt, so re-validating from the dashboard
   keeps working.
3. The 200 is sent before any work starts — Zoom's delivery deadline is
   seconds, a pipeline run is minutes. **The 200 promises a run, not a board.**
4. On a worker thread: open the signalling socket, handshake, take the media
   URL from the response, open the media socket, handshake again, then read
   transcript frames.

### The gates, carried over from `meetings/runner.py`

- A request whose quote is **not literally in the transcript** is dropped —
  `meetings.intent.extract_requests` does this, and `zoombot/agent.py` calls it
  rather than re-implementing it.
- A request below `DEFAULT_CONFIDENCE_FLOOR` (0.6) is recorded in
  `ZoomReport.considered` and **not built**. "We could just use a 5 volt rail"
  is not an order.
- `max_runs_per_meeting` caps paid runs, and the requests past the cap are
  *said out loud* as capped rather than vanishing.
- **Nothing is ever ordered.** No module in this package imports an ordering
  path.

Live speech arrives in fragments, so `zoombot/agent.py` gathers chunks into
windows before asking the model: a window ends at `WINDOW_GAP_MS` (15 s) of
silence or `WINDOW_MAX_CHARS` (4000), the last `WINDOW_OVERLAP_CHUNKS` (2)
chunks are repeated at the head of the next window so a requirement split
across a boundary stays quotable, and a window under `WINDOW_MIN_CHARS` (40) is
not sent at all because every window is a paid model call. Overlap makes
duplicates certain, so requests are de-duplicated on their quote before the cap
is applied.

Nothing returns a quiet zero: a stream that was never authorised raises
`HandshakeError`, one that dies mid-way raises `StreamInterruptedError` (with
the count of chunks that did arrive), and one that ends cleanly having carried
no transcript raises `EmptyStreamError` — "we were not allowed to listen",
"half a meeting" and "nobody spoke" stay three different facts.

---

## Speaking, and saying which way it went

`ZOOM_SPEAK_MODE` picks a `Speaker`, and `ZoomReport.spoke_via` records the
name of the speaker object that was actually used — read off the object, never
inferred from the configuration:

| Mode | Speaker | `name` | Audible in the room? |
|---|---|---|---|
| `sdk` | `MeetingSdkSpeaker` | `meeting_sdk` | Yes — and only if the container in `bot/` is running and implemented |
| `chat` | `ChatSpeaker` | `zoom_chat` | No. Text in the meeting chat, which nobody may look at |
| `off` | `NullSpeaker` | `null` | No. Records what would have been said |

`speak.AUDIBLE_SPEAKERS` is the single whitelist of names whose output made a
sound (`{"meeting_sdk"}`), and `speak.is_audible(name)` is the one question a
report reader is really asking. It is a whitelist rather than a flag on the
object, because a flag is something a future speaker can set optimistically
about itself.

Failures are named, never swallowed: `SpeakError` carries a `code` —
`bot_unreachable`, `bot_refused`, `bad_host`, `redirect_refused`, `zoom_auth`,
`zoom_api`, `no_voice`, `empty_text`, `bad_speak_mode`. A speaker failure is
recorded as a warning on the report and never takes the run with it; the board
is the product.

### The `bot/` container

`zoombot/bot/` is the headless Meeting SDK participant: a Dockerfile, an
entrypoint, `control_stub.py` and a README that states the whole prerequisite
list. It exposes a **loopback-only, unauthenticated** control surface on
`127.0.0.1:8781` (`speak.BOT_CONTROL_URL`), which is why
`speak._ensure_loopback_url` refuses any control URL that is not loopback —
anything that can reach that port can make the bot talk in a meeting.

| Method | Path | Body | Response |
|---|---|---|---|
| `GET` | `/healthz` | — | `{"ok": true, "joined": bool, "meeting_id": str}` |
| `POST` | `/say` | `{"meeting_id", "text"}` | `{"spoken": true}` or `{"spoken": false, "error": …}` |
| `POST` | `/audio` | `audio/wav` bytes, `X-Zoom-Meeting-Id` header | `{"spoken": true}` |

`spoken` is `true` only when audio actually reached the SDK's raw audio sender.
Text-to-speech is a seam on both sides: pass a `Voice` callable to
`MeetingSdkSpeaker(voice=…)` and WAV bytes are posted to `/audio`, or let the
container synthesise with whatever `TTS_CMD` names. The documented production
choice is [Piper](https://github.com/rhasspy/piper) (MIT, offline, CPU, 16-bit
mono PCM WAV). Nothing in this package ships or downloads a TTS model.

`zoombot/bot/README.md` lists the seven steps to actually try it, and its first
line stands until someone has done step 7 — joined a meeting and *listened*.

---

## Security boundaries

- **Host allowlist on every URL**, including the signalling URL Zoom itself
  hands back: an authenticated body is not a trusted one. `ALLOWED_HOSTS` is
  `api.zoom.us` / `zoom.us` exactly; the per-stream media hosts are allocated
  dynamically, so `ALLOWED_HOST_SUFFIXES` (`.zoom.us`, `.zoom.com`) is matched
  against `urlsplit().hostname` — never the userinfo, never the query.
- **Redirects are refused** on every transport. The stdlib would copy the
  `Authorization` header onto the new request and send it wherever the 3xx
  points.
- **URLs in error text have their query stripped** — an RTMS URL's query
  carries a token.
- **Bounded everywhere**: 1 MiB webhook bodies, 1 MiB frames, 20 000 frames per
  stream, 32 frames while waiting for a handshake, a 15 s socket timeout and a
  240 s wait for a run slot. One concurrent run by default.
- Run memory is **in-process only**. After a restart the surface says so
  (`runner.NO_RUN_REMEMBERED`) rather than answering about whichever board
  happened to be last.

---

## Tests

`zoombot/tests/` — `test_zoom_config.py`, `test_zoom_rtms.py`,
`test_zoom_speak.py`, `test_zoom_agent.py`, `test_zoom_runner.py`,
`test_zoom_http.py`. All offline: a recorded `Connection` drives the stream
(including the interesting case, a socket that dies mid-frame), a recorded
`Transport` drives the REST calls, and `ScriptedModel` drives the extraction.
They are in `testpaths`, so `python -m pytest -q` runs them, and
`ruff check … zoombot teamsbot` lints them. The basenames carry a `zoom_`
prefix because `scripts/check_docs.py` keys per-module counts by basename.

Nothing in the suite builds the container, starts it, or reaches Zoom.
