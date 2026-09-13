# Hardy

Hardy is [silkscreen](../README.md)'s desktop app — an AI hardware engineer
on your desktop, and a completely reimagined UI for the board-generation
engine, built as a floating always-on-top overlay plus a review workbench.
You describe a board in a sentence; the engine designs the PCB — schematic,
placement, routing — and Hardy walks it through review toward ordering,
the way a colleague would slide a board across the bench.

About the name: it is deliberately a human-feeling name rather than a product
name. The app is supposed to feel like a colleague at the bench, not a tool,
and a tool name would have worked against that from the first menu entry.

**Kaleo** and then **Ada** were this app's earlier names. Kaleo survives only as
internal identifiers that are expensive to change and invisible to users — the
Tauri bundle id `com.silkscreen.kaleo` (which owns the on-disk settings
directory), the npm package and Rust crate names, `KALEO_*` environment
variables, `~/.kaleo/`, and the `kaleo.*` storage keys. Ada survives as the
spoken wake word ("hey Ada", `WAKE_WORD` in `src/lib/wake-word.ts` and the
trained `resources/wake/hey_ada.onnx` model), as `ADA_REPO_ROOT`/`ADA_PYTHON`,
and in internal identifiers such as the terminal's `ada` destination. Nothing
else a user reads should say Kaleo or Ada; if you find one that does, it is a bug.

## Lineage and licence

Hardy is a hard fork of [Pluely](https://github.com/iamsrikanthnani/pluely)
by Srikanth Nani (Tauri 2 + React 19 + TypeScript + Tailwind v4 + shadcn/ui),
forked at v1.0.0. Full attribution, the fork point, and a dated log of every
change from upstream live in [NOTICE.md](./NOTICE.md); upstream's own README
is preserved unmodified as [UPSTREAM-README.md](./UPSTREAM-README.md).

The licence boundary matters more than usual here. Pluely is GPL-3.0, so this
directory — and any binary built from it — is GPL-3.0, inside a repository
that is otherwise MIT. What keeps the Python engine MIT is process
separation: Hardy talks to the engine only over HTTP (`/healthz`,
`/generate`, `/generate/stream`), exactly as the web UI does — two programs
that communicate, not one program in two languages. Never copy code across
the boundary in either direction; if the app needs something the API does not
expose, extend the API. NOTICE.md states the full rules and is the authority
if this paragraph and it ever disagree.

## Running it

Hardy is a client. Start the engine first, from the repository root:

```bash
# one-time setup
python -m venv .venv && ./.venv/bin/pip install -e ".[dev,agents,cloud,adk]"

# easiest: loads .env, starts the service, opens a browser
./.venv/bin/silkscreen serve --port 8081

# or run the service module directly — it does NOT read .env, so export the key
set -a && . ./.env && set +a
PORT=8081 python -m service.app
```

`GOOGLE_API_KEY` must be in the process environment for the module form:
`service/app.py` deliberately has no dotenv loader (only the CLIs do), and a
keyless service answers every `/generate` with a 502 naming the variable.

Then, in this directory:

```bash
npm install
npm run tauri dev
```

`npm run tauri dev` needs a Rust toolchain (rustup, plus the platform's Tauri
prerequisites). The machine this port was written on does not have one, so
**nothing here has been compiled yet** — the TypeScript surfaces typecheck,
but no Tauri binary has been built or run. Treat the first successful
`tauri dev` as an open task, not a formality.

## Menu bar

Hardy also sits in the macOS menu bar (the system tray on Windows and Linux)
beside the other status icons. The glyph is the mic state: an outline ring
while Hardy is muted, a filled orb while the microphone is open. Left click
shows or hides the overlay — the same toggle as the global shortcut. Right
click opens the quick actions: **Show/Hide Hardy**, **Hardy listening** (the
bar's mic switch; the check mark follows the real microphone, so it moves
only once the ear has actually opened or closed), **Open dashboard**, **About
Hardy** (the version) and **Quit Hardy**. The labels are constants in
`src-tauri/src/tray.rs`, pinned by its unit tests.
On macOS the icon is a template
image, so it takes the menu bar's light or dark tint like its neighbours.
Source: `src-tauri/src/tray.rs`; glyphs and the script that drew them in
`src-tauri/icons/tray/`.

## Keys, and setting up a team

The mechanics of the Gemini key live in
[docs/install.md](../docs/install.md#google_api_key-what-it-is-for); this
section is what is specific to Hardy and to sharing a checkout with teammates.

**Hardy never sees the Gemini key.** The key belongs to the Python engine's
process environment on each machine — the app is an HTTP client on loopback
and has nowhere to put a key by design. Each teammate puts `GOOGLE_API_KEY`
in their own repo-root `.env` (gitignored) and starts the engine as above. A
missing key is a distinct error state in the UI ("setup", not "outage") that
says exactly this.

**Never commit a key.** `.env` is gitignored for a reason; a key that touches
git history is burned even after deletion. Share a team key out-of-band (a
password manager), or better, have each person cut their own free key.

**Other providers' keys do nothing here.** The pipeline is Gemini through the
engine's `Model` protocol, and the hackathon rules require Gemini via the
Gemini API or Vertex — a Cerebras/OpenAI/etc. key has nothing to plug into.
The custom-AI-provider screens still visible under the dashboard are
leftover Pluely chat plumbing scheduled for removal, not a supported path;
keys entered there configure a chat feature Hardy does not use.

**A deployed team engine** (Cloud Run, one shared instance) is gated by a
shared access token rather than per-person Google keys — the service holds
the Gemini key server-side and refuses requests without the token (service
PR #21). Hardy's Engine page takes an optional access token alongside the
address; the URL validator accepts remote engines over HTTPS only, so the
token never travels unencrypted, while plain-http stays loopback-only.
As of 2026-09-06 the Tauri capability (`src-tauri/capabilities/default.json`)
allows any `https://` origin as well, so a deployed engine is reachable in
principle, but that path has not been exercised against a built app yet — see
`NOTICE.md`.

## State of the port

Honestly: this is a fork mid-surgery. The silkscreen surfaces — the engine
client under `src/lib/silkscreen/`, the review workbench, the identity in
`src/config/` — are new. Underneath them, Pluely's original chat, STT/voice,
and screenshot surfaces are still present in the tree and still wired into
parts of the app. They are slated for removal, not integration; until that
pass lands, expect Pluely-branded strings and dead provider-configuration UI
in corners the new surfaces have not replaced. Upstream's updater endpoint
and analytics have been disabled in config (see NOTICE.md). The icons are no
longer Pluely's: `src-tauri/icons/`, `src/assets/kaleo-mark.png` and
`images/kaleo-logo.svg` all carry the current mark — a differential pair, two
parallel stepped traces in charcoal and copper. (The `kaleo-*` filenames are
retired-name artefacts, not a second mark.)
