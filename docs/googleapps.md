# Google Workspace integration (`googleapps/`)

`python -m googleapps` delivers a finished pipeline run to Google Chat, Gmail,
and Google Calendar. It is stdlib-only — no `google-auth`, no
`google-api-python-client` — and calls `silkscreen.agents.generate_pcb`
exactly as the CLI does.

> **Honesty first:** this integration was written offline against the
> documented REST surface and has **never been run against live Google APIs**.
> The offline tests pin request construction — URLs, PKCE, MIME, the card
> payload — against a recorded transport. A wrong scope name or a payload
> field Google rejects would only show up on the first live run. Treat that
> run as the real test.

## What each destination needs

| Destination | Credential | Env var |
|---|---|---|
| Chat space card (`--chat`) | Incoming-webhook URL (the URL **is** the secret) | `GOOGLEAPPS_CHAT_WEBHOOK` |
| Gmail send (`--email`) | OAuth Desktop-app client + one-time `auth` | `GOOGLEAPPS_CLIENT_ID`, `GOOGLEAPPS_CLIENT_SECRET` |
| Calendar review event (`--schedule`) | same OAuth client and token | same |
| The pipeline itself (`run`) | Gemini API key | `GOOGLE_API_KEY` |

The three are independent: you can post Chat cards with no OAuth client at
all, or email results with no webhook. `python -m googleapps` reads `.env`
from the working directory through the same loader as `python -m silkscreen`,
so a repo-root `.env` serves both (the service still does not read `.env`).

## 1. Create (or reuse) a Google Cloud project

The hackathon project that already holds your `GOOGLE_API_KEY` works fine —
Gmail/Calendar OAuth and the Gemini key do not conflict. Otherwise create one
at <https://console.cloud.google.com/projectcreate>.

## 2. Enable the two APIs

**APIs & Services → Library**, enable:

- **Gmail API**
- **Google Calendar API**

(Chat webhooks need no API enablement — the webhook is created in the Chat
space itself, step 4.)

## 3. Create the OAuth client (type: Desktop app)

1. **APIs & Services → OAuth consent screen**: configure it once (External is
   fine for a personal account; add yourself as a test user while the app is
   unverified).
2. **APIs & Services → Credentials → Create credentials → OAuth client ID**,
   application type **Desktop app**.
3. Copy the **Client ID** into `GOOGLEAPPS_CLIENT_ID` and the **Client
   secret** into `GOOGLEAPPS_CLIENT_SECRET` (in `.env` or the environment —
   never into code). A Desktop-app client secret is not treated as
   confidential by Google's model — the flow's security comes from PKCE — but
   keep it out of the repo all the same.

The requested scopes are exactly two, and neither can read your mailbox:
`gmail.send` and `calendar.events`.

Google's consent screen shows each of those as its **own checkbox** (granular
consent), so a sign-in can succeed with one of them declined. What Google says
it granted comes back in the token response's `scope` field, and that is what
is recorded beside the token and believed afterwards: `check` prints which
destination each grant covers, `GET /deliver/config` (and therefore
`/integrations` and `/setup`) closes only the destination whose box was
unticked, and `run --email` / `--schedule` refuse *before* the pipeline spends
a model call rather than at the API call after the board exists. A refresh
response usually omits `scope`, which per RFC 6749 §5.1 means unchanged, so
the record carries over rather than being blanked. Sign in again to change a
grant — the consent URL sends `prompt=consent` (without it Google shows an
already-authorised user no screen and returns no refresh token) and
`include_granted_scopes=true`, so a later, narrower authorisation cannot drop
a scope you already granted. A token file written before any of this was
recorded reports its grant as *not recorded* and keeps working; it is not
treated as a refusal.

## 4. Create the Chat incoming webhook

In the Chat space that should receive run cards: **space name → Apps &
integrations → Webhooks → Add webhook**. Name it (e.g. `silkscreen`), copy
the generated URL into `GOOGLEAPPS_CHAT_WEBHOOK`.

The URL embeds the credential — its last characters *are* the token. Never
commit it, paste it into a chat, or log it; `check` and every error message
show only that it is set and its length, not even a tail. The code refuses
to send to anything that is not `https://chat.googleapis.com/v1/spaces/…`.

## 5. Sign in

```bash
python -m googleapps auth
```

This opens Google's consent page in your browser, catches the redirect on a
`127.0.0.1` loopback port, exchanges the code (PKCE S256; the verifier never
touches disk), and writes the token to
`~/.config/silkscreen/google-token.json` (override with
`GOOGLEAPPS_TOKEN_PATH`) with mode **0600**. The access token refreshes
transparently on later runs; if the refresh token is ever revoked, the error
tells you to run `auth` again.

Check the result — this makes no network call:

```bash
python -m googleapps check
```

## 6. Run

```bash
python -m googleapps run "a 3.3V LDO board with an AMS1117-3.3" \
    -o out/board.kicad_pcb \
    --chat \
    --email lead@example.com --email james@example.com \
    --schedule --attendee lead@example.com
```

- `--chat` posts a cardsV2 card: verdict, stage timings, board size and
  solver status, review counts with blocker titles, and **every unrouted net
  named verbatim** with the router's reason. A card never says "board ready"
  over a ratsnest.
- `--email` sends a plain-text summary with the emitted `.kicad_pcb`
  attached. The file is refused locally above 15 MB: Gmail's cap is 25 MB
  for the whole *encoded* message, and base64 makes the attachment a third
  larger on the wire, so 15 MB of file is about 20 MB sent. A test builds the
  largest allowed message and measures it against the cap.
- `--schedule` creates a Calendar event titled after the board with a Meet
  link (`conferenceData.createRequest`) and your `--attendee` list — **only
  when the adversarial review found blockers**. A clean review prints
  "no blockers — no review event was created" and schedules nothing. The
  insert carries `sendUpdates=all`, so every attendee receives the
  invitation; without it Google creates the event and tells nobody.

The pipeline flags are the CLI's, under the CLI's names: `-d PART=URL`
(repeatable), `--model`, `--repairs`, `--time-limit`, `--no-review`,
`--no-route`.

**Everything that can fail before the paid run, fails before it.** `run`
checks the API key, the webhook's shape, every `--email` / `--attendee`
address (plain addresses only — anything that could fold or split a MIME
header is refused), the `--datasheet` syntax, and — when Gmail or Calendar
is asked for — that the stored token will still be usable *after* the run:
it must carry a refresh token and the OAuth client must be configured,
because a pipeline can outlast an access token's remaining minutes. An
expired token is refreshed here, the one pre-run step that may touch the
network; a revoked refresh token is reported here, with the `auth` command
that fixes it, rather than after the board exists.

Delivery failures are independent: a rejected card does not stop the email,
and every failure is reported with Google's own error message and an exit
code reflecting it.

## 7. From the desktop overlay

The Ada overlay's step mode (`service/steps.py`) shows a "Send it on" panel
under the step list once the board is routed. It uses three service routes
(`service/deliver.py`) over the same package:

- `GET /deliver/config` — what is configured, with the exact fix for each
  gap; never a secret. Reports `oauth_client` / `signed_in` so the panel can
  offer a consent button when the OAuth client is set but no token exists.
- `POST /deliver/auth/start` — returns `{auth_url}`; Ada opens it with Tauri
  (the service often cannot open a browser from a worker thread).
- `POST /deliver/auth` with `{client_opens: true}` — waits for the loopback
  redirect and returns a fresh config. Without `client_opens`, the service
  opens the browser itself (CLI-style fallback, same as
  `python -m googleapps auth`).
- `POST /steps/<id>/deliver` with `{chat, email: [...], schedule, attendees:
  [...]}` — the card, the email with the `.kicad_pcb`, the review event, each
  reported on its own. The Calendar rule is unchanged: blockers only, and it
  says so when the review step has not run.

The service does **not** read `.env`: export `GOOGLEAPPS_CHAT_WEBHOOK`,
`GOOGLEAPPS_CLIENT_ID`, `GOOGLEAPPS_CLIENT_SECRET` (and `GOOGLEAPPS_TOKEN_PATH`
if not the default) into the environment that launches `python -m service.app`.
With the client id and secret set, press **Sign in with Google** in Ada's Send
panel — Ada opens the consent tab. (`python -m googleapps auth` remains a CLI
fallback.) Chat still needs only the webhook; it does not use the OAuth token.

## Security properties, enforced by test

- Tokens, client secret, and the webhook URL are never printed or logged;
  the client id is masked to a short tail, the others show only a length.
  Google's error messages are shown verbatim because they never echo a
  credential.
- HTTP redirects are refused, so a 3xx from a Google host can never carry
  the bearer token to another host.
- The token file is written atomically (a 0600 sibling moved into place)
  and the mode is re-asserted on every rewrite. (On Windows,
  where POSIX mode bits do not exist, the file relies on the user profile's
  ACLs and `check` does not warn.)
- Card text is HTML-escaped before it reaches Chat's `textParagraph`, which
  renders a subset of HTML — a net named `<3V3` would otherwise vanish.
- Recipient and attendee addresses are validated before they reach a
  header: no whitespace, commas, quotes, brackets or newlines.
- The PKCE `code_verifier` exists only in process memory.
- An exact-match host allowlist (`oauth2.googleapis.com`,
  `gmail.googleapis.com`, `www.googleapis.com`, `chat.googleapis.com`) is
  checked before any request is handed to the transport — a suffix-spoofed
  host like `chat.googleapis.com.evil.example` is refused.
- No credential exists anywhere in the code; everything arrives via the
  environment.
