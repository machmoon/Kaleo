# The Teams calling bot (unbuilt half)

**Status: skeleton. Not built or run by the test suite, and never run against a
live Microsoft 365 tenant from this repository.** Read this before you believe
anything else in here works.

`teamsbot/` as a whole can already do two things offline-testably: read a
finished meeting's transcript through Graph (`teamsbot/graph.py`) and post into
a meeting chat (`teamsbot.speak.ChatSpeaker`). What it cannot do without the
container described here is **speak out loud in a live meeting**. That needs a
real participant on the call with a media session, which is what a Graph
*calling bot* is.

This directory holds the control plane for that participant:

| File | What it is |
|---|---|
| `control.py` | The local HTTP surface `teamsbot.speak.CallingBotSpeaker` drives: `GET /healthz`, `POST /say`. |
| `Dockerfile` | Builds `control.py` into a container. Standard library only, non-root, loopback-intended. |

`POST /say` answers **501 Not Implemented** with a sentence naming what is
missing. That is deliberate. The alternatives were: answer 200 (a run report
would then claim the agent spoke in a meeting where no audio ever existed),
answer nothing (an unexplained timeout), or ship a plausible-looking media stub.
A named 501 surfaces through `CallingBotSpeaker` as
`SpeakError("bot_refused", …)` and lands in the report a person reads.

## What is not here, and why

The media plane — joining a Teams call, negotiating a media session, pushing
audio frames — needs Microsoft's Graph Communications calling and media
libraries (`Microsoft.Graph.Communications.Calls.Media`). Those are .NET
components with their own platform requirements and licensing, and the shape to
copy is `microsoft/BotBuilder-Samples` (the calling-bot samples) together with
the Graph `communications/calls` REST surface. Vendoring a partial copy here
would produce exactly what this repository refuses to ship: something that looks
like it works.

## What an operator actually needs

Everything below is a real prerequisite for a Teams calling bot. None of it can
be arranged from inside this repository, and none of it has been done here.

1. **An Entra ID (Azure AD) app registration** — its client id and secret are
   `TEAMS_APP_ID` / `TEAMS_APP_SECRET`, and the directory is
   `TEAMS_TENANT_ID`.
2. **Application permissions, admin-consented** — the calling set, which is
   substantial and should be granted deliberately:
   `Calls.JoinGroupCall.All` (join a meeting), `Calls.AccessMedia.All` (the one
   that lets the bot hear and speak — a tenant-wide capability, and the reason
   an administrator will ask questions), plus, for the transcript half this
   package already uses, `OnlineMeetings.Read.All` and
   `OnlineMeetingTranscript.Read.All`.
3. **A Teams application access policy**, granted with PowerShell
   (`New-CsApplicationAccessPolicy` / `Grant-CsApplicationAccessPolicy`) for
   the users whose meetings the bot may act in. Without it Graph answers 403
   for a meeting the app registration otherwise has permission for — a failure
   that reads as a bug and is a policy.
4. **A publicly reachable https callback endpoint** — `TEAMS_BOT_ENDPOINT`,
   which is where Teams POSTs call notifications to `teamsbot/app.py`'s
   `/api/calls`. A tunnel (ngrok, dev tunnels) is the usual development answer;
   the certificate must be real, and the token on those requests is validated
   before the body is parsed.
5. **A Bot Framework / Azure Bot resource** registered against the same app id,
   with the Teams channel and calling enabled and pointed at the same endpoint.
6. **Media ports and a media platform certificate** for the container, per
   Microsoft's calling-bot deployment documentation — the part that is
   genuinely awkward to run outside Azure.

## Trying it

```bash
docker build -t silkscreen-teams-callingbot teamsbot/bot
docker run --rm -p 127.0.0.1:8791:8791 silkscreen-teams-callingbot
curl -s http://127.0.0.1:8791/healthz
# {"ok": true, "service": "silkscreen-teams-callingbot", "can_speak": false}
curl -s -XPOST http://127.0.0.1:8791/say \
  -H 'content-type: application/json' \
  -d '{"meeting_id":"19:meeting_x@thread.v2","text":"hello"}'
# 501, with the sentence naming the missing media stack
```

With `TEAMS_SPEAK_MODE=sdk`, `teamsbot` builds a `CallingBotSpeaker` pointed at
`http://127.0.0.1:8791` (override with `speaker_for(..., control_url=…)`). With
the container stopped, saying something raises
`SpeakError("bot_unreachable", …)`; with it running, `SpeakError("bot_refused",
…)` carrying the 501's sentence. Neither is ever silence, and neither is ever
reported as having spoken.

**Bind it to loopback.** The control surface has no authentication of its own —
it is a sidecar to the process holding the call — which is why
`teamsbot.speak.ensure_control_url` refuses plaintext http to anything but
loopback. Anything that can reach this port can make the bot talk in a meeting.

## To make it real

Replace `say()` in `control.py`. That is the whole contract: given a meeting id
and a sentence, put that sentence into that call's audio, or raise. Everything
above it — the HTTP shape, the speaker, the report line that names which
speaker was used — already accounts for both outcomes.
