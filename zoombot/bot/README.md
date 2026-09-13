# `zoombot/bot/` — the headless Zoom Meeting SDK participant

**Status: not built, not run, and never tested against a live Zoom account.**

Nothing in this directory is built or executed by the test suite. No image has
been built from this `Dockerfile` in this repository, no container has been
started from it, and no line of audio has ever been sent into a real Zoom
meeting from this code. The Python half (`zoombot/speak.py`'s
`MeetingSdkSpeaker`) is tested offline against a recorded transport; that
proves the *client* speaks the control protocol described below, and proves
nothing about the container. Treat everything here as a specification and a
starting point, not as a working bot.

If you want the half that plausibly works today, use `ZOOM_SPEAK_MODE=chat`,
which posts into the meeting chat over the ordinary Zoom REST API and needs
none of this.

## Why a container exists at all

Zoom's Realtime Media Streams (RTMS) are **receive-only**. RTMS will hand you a
live meeting's audio and transcript; there is no REST call, no webhook reply
and no RTMS message that makes a sound in a meeting. The only supported way to
emit audio into a Zoom meeting is to *be a participant*, which means running
the Zoom Meeting SDK.

So this is a headless Linux participant that joins the meeting, and exposes a
tiny local control surface that `MeetingSdkSpeaker` drives. It is modelled on
Zoom's own open-source sample:

- <https://github.com/zoom/meetingsdk-headless-linux-sample> — the
  officially-maintained headless C++ participant, including its raw audio
  sender. Prefer its shape over anything clever; when this directory disagrees
  with upstream, upstream is right.
- <https://github.com/zoom/rtms> — the RTMS samples, for the receive half
  (`zoombot/rtms.py`), not for anything here.

## The SDK binary is not vendored here

The Zoom Meeting SDK for Linux is downloaded from the Zoom App Marketplace
under Zoom's own licence terms. It is **not** in this repository and must not
be committed to it. The `Dockerfile` expects you to place the archive you
downloaded at `zoombot/bot/vendor/zoom-meeting-sdk-linux.tar.xz` before
building; the build fails with a message saying exactly that if it is missing,
rather than pretending to work.

## Credentials and scopes it needs

Create a **Meeting SDK** app *and* a **Server-to-Server OAuth** app in the Zoom
App Marketplace. They are different app types and this integration wants both:

| Purpose | App type | Value | Env var |
|---|---|---|---|
| Sign the SDK join request (JWT) | Meeting SDK | Client ID | `ZOOM_SDK_KEY` |
| Sign the SDK join request (JWT) | Meeting SDK | Client secret | `ZOOM_SDK_SECRET` |
| Mint REST tokens (chat, meeting reads) | Server-to-Server OAuth | Client ID | `ZOOM_CLIENT_ID` |
| Mint REST tokens | Server-to-Server OAuth | Client secret | `ZOOM_CLIENT_SECRET` |
| Mint REST tokens | Server-to-Server OAuth | Account ID | `ZOOM_ACCOUNT_ID` |
| Verify webhooks | either, webhook feature | Secret token | `ZOOM_WEBHOOK_SECRET_TOKEN` |

Scopes, kept to the minimum an admin reading the consent screen would accept:

- `meeting:read:meeting:admin` — resolve a meeting the bot is asked to join.
- `meeting:write:chat_message:admin` — only if you also use
  `ZOOM_SPEAK_MODE=chat`. The bot itself does not need it.
- Meeting SDK apps carry no OAuth scopes; joining is authorised by the JWT the
  container signs with `ZOOM_SDK_KEY`/`ZOOM_SDK_SECRET`, plus the meeting
  passcode or a ZAK for a host-authenticated join.

To send audio the SDK app must be permitted **raw data / local recording**
access. In Zoom that is an account-level setting plus, in most accounts, the
host granting recording permission to the participant. A bot that joins and
cannot get raw audio permission stays silent — and, per the honesty rule, must
report that rather than appear to have spoken.

Participants must be told a bot has joined. Zoom's own recording-consent
notice covers this in most configurations; check your jurisdiction. Do not
disable the join chime.

## The control surface `MeetingSdkSpeaker` drives

Loopback HTTP on `127.0.0.1:8781` (`BOT_CONTROL_URL` in `speak.py`). It is
**unauthenticated**, which is why the Python client refuses any control URL
that is not loopback: never publish this port, never map it to `0.0.0.0`.

| Method | Path | Body | Response |
|---|---|---|---|
| `GET` | `/healthz` | — | `{"ok": true, "joined": bool, "meeting_id": str}` |
| `POST` | `/say` | `{"meeting_id": str, "text": str}` | `{"spoken": true}` or `{"spoken": false, "error": "..."}` |
| `POST` | `/audio` | `audio/wav` bytes, `X-Zoom-Meeting-Id` header | `{"spoken": true}` |

The response rule is the whole point: `spoken` is `true` only when audio was
actually handed to the SDK's raw audio sender. Anything else answers
`{"spoken": false, "error": ...}` and the client raises. A container that
returned `{"spoken": true}` optimistically would make `zoombot`'s run report
claim the agent spoke out loud in a meeting where it did not.

## Text to speech

`/say` needs a voice. It is a seam on both sides:

- Python side: pass a `Voice` callable to `MeetingSdkSpeaker(voice=...)` and
  synthesis happens before the request, which then posts WAV bytes to
  `/audio`. Nothing in this package ships or downloads a TTS model.
- Container side: `/say` synthesises with whatever `TTS_CMD` names.

The documented production choice is **Piper**
(<https://github.com/rhasspy/piper>, MIT): actively maintained, runs offline on
CPU, and writes 16-bit mono PCM WAV, which is what the Meeting SDK's raw audio
sender consumes after resampling to 32 kHz mono. A reasonable `TTS_CMD`:

```
TTS_CMD=piper --model /voices/en_US-lessac-medium.onnx --output_file -
```

Any command that reads text on stdin and writes a WAV on stdout works. The
choice is the operator's; hard-wiring a cloud TTS key into a package that
otherwise needs none is a secret and a bill nobody asked for.

## What a person must actually do to try this

1. Create the two Zoom apps above and note the five values.
2. Download the Zoom Meeting SDK for Linux from the Marketplace and put the
   archive at `zoombot/bot/vendor/zoom-meeting-sdk-linux.tar.xz`.
3. Get a TTS binary and a voice model (Piper, above) and mount them at
   `/voices`.
4. Write the C++ (or Node) participant against
   `zoom/meetingsdk-headless-linux-sample`, exposing the three endpoints in the
   table. `entrypoint.sh` here starts a placeholder that refuses every request
   with `{"spoken": false, "error": "the participant is not implemented"}` —
   deliberately, so that a half-finished container cannot look like a working
   one.
5. `docker build -t kaleo-zoombot ./zoombot/bot` and run it with
   `--network host` (or publish `127.0.0.1:8781:8781` only) and the env above.
6. Set `ZOOM_SPEAK_MODE=sdk` for the Python side.
7. Join a meeting you own, run `zoombot`, and **listen**. Green tests here mean
   nothing about step 7; the only proof is hearing it.

Until someone has done step 7 and said so, this README's first line stands.
