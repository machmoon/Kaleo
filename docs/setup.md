# Setup Assistant

The desktop app (`app/`) gates its first launch behind a macOS-style Setup
Assistant: the strip stays hidden, the dashboard window opens at `/welcome`,
and the engineer walks six screens (Hello, Choose your look, Start the engine,
Connect accounts, Permissions, You're all set). Four of those draw a progress
dot: `hello` hides the row and `done` has no footer, so they are outside the
count (`DOTTED_STEPS` in `app/src/lib/setup/machine.ts`, the rule read from
`vendor/openwhispr/src/components/onboarding/flow.ts`'s `COMPACT_STEPS`).
Every screen has "Set Up Later";
closing the window mid-way counts as skipping the rest. Settings offers "Run
Setup Again". The wizard's service half is `service/setup.py`; the persistence
half is `service/envfiles.py`; the shell half is `app/src-tauri/src/setup.rs`.

Status, stated plainly, and **re-checked 2026-09-08**. This paragraph used to
say the Rust half existed only as a patch at `~/Desktop/Coding/kaleo-shell-setup.patch`
and had never been compiled. That is no longer true, and the patch file is
gone: `app/src-tauri/src/setup.rs` was committed in `d9cad3d` ("Ada desktop
app: the strip, the Setup Assistant, wake and desk", 2026-09-07), it is in the
module tree (`mod setup;` at `app/src-tauri/src/lib.rs:4`), its five commands
are registered (`lib.rs:96-100`: `setup_status`, `setup_finish`,
`setup_restart`, `setup_window_mode`, `kaleo_has_focus`), `apply_launch_policy`
runs at `lib.rs:115`, and `tauri-plugin-store` / `tauri-plugin-notification`
are both in `app/src-tauri/Cargo.toml`. So the gate, the notifications and
`settings.json` are all in the source of the shell.

It has also been compiled: TODO.txt feature 26 records the patch applied
2026-09-06 with `cargo check` clean and 25 Rust tests, and `tauri dev` run and
looked at on 2026-09-07 — the gate fires, the strip stays hidden, `/welcome`
opens with a Dock icon. What remains open there is narrower and worth keeping
in view: **only two of the six screens have ever been looked at** (hello and
permissions), there is no `cargo check` step in CI for `app/src-tauri`, and
there is no `app/src-tauri/target/` in this working tree, so a build here
starts cold. Anything visual in the other four screens is unproven, and
TODO.txt feature 26 carries the two traps that make re-running the wizard
harder than deleting `settings.json`.

## Two modes, one UI

`KALEO_SETUP_MODE=demo` on the service makes every "connect" a rehearsal.
Nothing reaches Google, Microsoft or Stripe; every response carries
`"mode": "demo"` and `"demo": true`; demo state lives under `~/.kaleo/demo/`
in files that start `# KALEO DEMO STATE -- not a credential` and carry
`KALEO_DEMO=1`. Those files are never read at service start and never exported
into the environment, so `GET /integrations` keeps saying `unconfigured` while
`GET /setup` says `ready, demo: true`. That asymmetry is the point: a demo
connection must never read as a real one anywhere. The wizard shows a
persistent banner in demo mode and the Done screen says nothing was actually
connected.

Live mode (the default) uses the same routes and the same UI. The mode is a
property of the service process, never a client flag, so a UI bug cannot mint
a "connected" state on a real install.

## Routes

All behind the bearer gate when `SILKSCREEN_ACCESS_TOKEN` is set, all
`Cache-Control: no-store`, every body stamped with `mode` and `demo`. `state`
uses the `/integrations` vocabulary: `ready | partial | unconfigured | unavailable`.

| Route | What it does |
|---|---|
| `GET /setup` | Aggregated status: `engine`, `google`, `microsoft`, `stripe`. Composes `deliver.config_report`, `integrations_report()["teams"]` and `billing.setup.setup_report`, so the three cannot disagree. |
| `POST /setup/google/connect` | Starts the PKCE loopback flow (`googleapps/auth.py`) on a thread; answers `202 {auth_url, job}`. In demo the URL is a loopback consent page. |
| `GET /setup/google` | Non-blocking poll: `job.state` is `idle | waiting | connected | failed`. |
| `POST /setup/google/disconnect` | Deletes the token file on this Mac. It does **not** revoke at Google; the response says so and names `myaccount.google.com/permissions`. |
| `POST /setup/google/credentials` | Saves `GOOGLEAPPS_CLIENT_ID/SECRET` to `~/.kaleo/google.env` after a shape check (`.apps.googleusercontent.com`). State stays `partial` until a token exists. |
| `POST /setup/microsoft/credentials` | One client-credentials token request against Entra through the `teamsbot/graph.py` allowlisted transport, then saves to `~/.kaleo/microsoft.env`. The verdict is a fixed vocabulary (`ok`, `status`, `code` such as `AADSTS7000215`, `meaning`), never Entra's free text and never the secret. |
| `GET /setup/microsoft`, `POST /setup/microsoft/disconnect` | Status and removal. Disconnect deletes only values the file supplied and reports `still_configured_from_environment`. |
| `POST /setup/stripe/credentials` | Delegates to `billing_routes.handle_config_post` with the live or demo transport and path. Demo accepts only the literal `rk_test_kaleo_demo` and never persists a key. |
| `GET/POST /setup/demo/consent` | The demo consent page. Public only in demo mode, exact-path match, nonce-gated (256-bit, single use, 10 minutes), `Host` must be loopback, no-store, nosniff, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`. |

What "connected" means per provider, in the words the UI uses:

- Google: "Signed in" only when a refresh token exists on this Mac. Demo: "Demo sign-in recorded. No Google account was contacted."
- Microsoft: "Token issued", never "Verified". Entra issued an app token for the registration; that proves the ids and the secret. It does not prove Graph permissions are consented or that `python -m teamsbot` is running. The `unverified` flag from `/integrations` is copied through.
- Stripe: "Key verified with Stripe. Webhook secret and price are not proven until a payment." Demo: "Demo billing. No key stored, no charge possible."
- Engine: "Answered /healthz at {url}", plus the `GOOGLE_API_KEY` row from `/config/status`.
- KiCad: "KiCad is here" only when `list_cli_tools` resolved the binary, with the caveat that Ada did not run it and the version is not known. "No KiCad on this Mac" names what stops working (showing a stage in KiCad, ERC/DRC, the order step's 3D export) and the `KICAD_CLI` fix, and does **not** block Continue — Ada designs boards without it. A machine that could not be asked (a browser tab, a build with no CLI allowlist) reads "KiCad: not asked", never "install KiCad".

The KiCad row also decides whether this screen auto-advances. A healthy,
keyed engine moves the wizard on after 600 ms; a missing canvas cancels that,
because a screen that dismisses itself in 600 ms cannot deliver bad news. That
is OpenWhispr's `required-models` rule
(`vendor/openwhispr/src/components/onboarding/flow.ts:159-161`, where the step
is spliced into the route only when the dependency is absent), applied inside
an existing screen rather than as a seventh one.

## Files on disk

```
~/.kaleo/                 0700
  billing.env             STRIPE_*                (was already here)
  google.env              GOOGLEAPPS_CLIENT_ID, GOOGLEAPPS_CLIENT_SECRET, GOOGLEAPPS_CHAT_WEBHOOK
  microsoft.env           TEAMS_APP_ID, TEAMS_APP_SECRET, TEAMS_TENANT_ID
  stripe_oauth.json       (billing/oauth.py)
  demo/                   0700, only ever written in demo mode
~/Library/Application Support/com.silkscreen.kaleo/settings.json   the app's own settings (tauri-plugin-store)
```

`service/envfiles.py` writes each file 0600 at creation (mkstemp then rename),
refuses a symlinked home or target, and reports any file it finds with a wider
mode as a hint in `GET /setup`. The service applies `~/.kaleo/*.env` into its
environment at start (`service/app.py main()` and `desktop/launcher.py`), with
**setdefault** semantics: a value already in the environment wins, and a save
that lands behind such a value answers `active: false, active_source:
"environment"` with the sentence "Saved and verified, but the running engine
still uses the value from its environment; restart it or remove that variable."
Precedence, highest first: process environment, `.env` (desktop launcher only),
`~/.kaleo/*.env`. `service.app` still does not read `.env`.

`KALEO_HOME` overrides `~/.kaleo` (tests point it at a temp dir). The demo
marker in file content, not the path, is what keeps a demo file out of live
use: `load_env` refuses any file carrying `KALEO_DEMO` when asked for live
values.

## Resetting

```bash
rm -r ~/.kaleo/demo                                                        # forget demo connections
rm ~/Library/Application\ Support/com.silkscreen.kaleo/settings.json       # see the first-launch gate again
```

## Walkthrough

```bash
set -a && . ./.env && set +a
KALEO_SETUP_MODE=demo PORT=8081 python -m service.app
cd app && npm run tauri dev
```

Delete `settings.json` first. Expect: no strip, the dashboard focused at
`/welcome` with a Dock icon, a demo banner on every step. Google's Connect
opens a loopback consent page in the browser; Allow flips the card. Microsoft
accepts GUID-shaped ids. Stripe's "Use the demo key" fills `rk_test_kaleo_demo`.
The notifications test button is the opt-in and posts the first banner; in a
`tauri dev` build macOS attributes it to "Terminal" (the plugin passes
`com.apple.Terminal` as the bundle id in dev), there is no click-back, and the
app gets no delivery signal, so the result line says "Sent", never
"Delivered". Done reveals the strip and shows one caption under it.

## Not built

See TODO.txt feature 26 for the full table. The short version: delegated
"Sign in with Microsoft", live coach marks inside the strip, notification
click-back, a menu-bar item, the `work_area()` positioning fix, a `cargo check`
CI step for `app/src-tauri`, Stripe Checkout inside the wizard, profiles, the
Cloud Run posture for `/setup`, and Windows file ACLs.
