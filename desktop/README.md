# Desktop development

Ada has two checkout-only desktop launchers. The native macOS shell under
`desktop/src-tauri/` is the path forward; `desktop/silkscreen-app` remains a
portable fallback that opens the same Svelte UI in a Chromium app window.

## Native macOS shell

Install Python and frontend dependencies, build the shared web bundle, then
run the Rust host:

```sh
./scripts/install.sh
cargo run --manifest-path desktop/src-tauri/Cargo.toml
```

The host starts `python -m desktop.sidecar` on an OS-assigned
`127.0.0.1` port, waits for `/healthz`, and injects that exact origin into the
embedded Svelte app. `CommandOrControl+Shift+K` hides or restores the window,
and text exports use the native Save dialog. Closing Ada closes the child
service and releases its port.

`ADA_REPO_ROOT` can select another checkout and `ADA_PYTHON` can select its
interpreter. By default the shell uses this repository and `.venv/bin/python`.

This is a developer shell, not a distributable app. Tauri bundling is disabled
until Python is packaged as a sidecar and the nested binaries are signed and
notarized. It currently uses a normal native window; the compact transparent
overlay remains the next milestone described in [TAURI.md](TAURI.md).

## Chromium app-window fallback

`desktop/silkscreen-app` runs silkscreen the way you would run an installed
application: one command, one window, no terminal to keep an eye on and no URL
to remember. It needs **no toolchain that the repo does not already have** —
Python plus a Chromium-family browser you already installed for other reasons.

```sh
cd frontend && npm install && npm run build && cd ..   # once, to build the bundle
./desktop/silkscreen-app
```

On Windows there is no shebang, so run the module directly:

```
.venv\Scripts\python desktop\launcher.py
```

## What it does

1. **Starts the service in-process**, bound to `127.0.0.1` on an
   OS-assigned port (`--port N` pins one). It uses `service.app.Handler`
   directly rather than `service.app.make_server`, which binds `0.0.0.0`
   because Cloud Run requires it — on a laptop that would publish an
   unauthenticated `/generate`, an endpoint that spends your Gemini quota, to
   everyone on the same wifi.
2. **Waits for `/healthz` to actually answer**, polling every 100 ms up to 30 s
   and requiring `{"ok": true}`. Nothing opens until the service is serving, so
   the window never lands on connection-refused.
3. **Opens the SPA as an application window** — the found browser launched with
   `--app=<url>`, which gives a window with no tab strip, no address bar and no
   bookmarks: the app's own title bar is the only chrome. It runs in a private
   profile under `desktop/.profile/` (gitignored).
4. **Shuts everything down cleanly.** Closing the window exits the launcher;
   Ctrl-C exits the launcher and terminates the window. Either way the server
   is `shutdown()` then `server_close()`d and the port is released.

It also reads the repo-root `.env` into the environment before starting
(`os.environ.setdefault`, so a real environment variable always wins).
`service/app.py` deliberately does not read `.env` — in Cloud Run the
environment *is* the environment — but on a desktop there is no deploy step to
export `GOOGLE_API_KEY`, and a window whose every run fails on a missing key is
not an app.

### Browser detection

App mode exists only in the Chromium family. Detection is per-platform,
because the thing you look for is different on each:

| Platform | How it looks | Why not the other way |
| --- | --- | --- |
| macOS | app-bundle paths under `/Applications` and `~/Applications` | The executable lives at `<App>.app/Contents/MacOS/<name>` and is on nobody's `PATH`, so `which` finds nothing however many browsers are installed |
| Windows | `%PROGRAMFILES%`, `%PROGRAMFILES(X86)%`, `%LOCALAPPDATA%` | Chrome installs per-machine or per-user depending on whether the installer had admin rights; a per-user install is on none of the machine-wide paths |
| Linux | `shutil.which` over the usual command names | `PATH` is the whole story; snap and flatpak both drop a wrapper there |

Chrome, Edge, Brave, Chromium and Vivaldi are all recognised. If none is
found, the launcher **says so on stderr** and falls back to `webbrowser.open`
— an ordinary tab. Opening a tab silently would make a missing Chrome look
like a launcher bug.

### Flags

| Flag | Effect |
| --- | --- |
| `--port N` | Bind a specific loopback port instead of an OS-assigned free one |
| `--no-browser` | Start the service, print the URL, open nothing (block until Ctrl-C) |
| `--profile DIR` | Use a different browser profile directory for the window |

`SILKSCREEN_PYTHON` overrides which interpreter the shim runs; by default it
prefers `.venv/bin/python`, because the system python almost never has
`ortools` and `kiutils` and the resulting `ModuleNotFoundError` reads as "the
app is broken" rather than "wrong interpreter".

## What the Chromium launcher deliberately is not

- **Not a native app.** It is a browser window in disguise. It has a browser's
  process model, a browser's memory footprint, and a browser's window
  decorations. Use the Tauri shell above for the native host.
- **Not always-on-top, not translucent, not hotkey-summoned.** Chrome's
  `--app` mode gives no control over window level, background transparency, or
  global shortcuts — those are exactly the capabilities that require a native
  shell. The SPA's glass skin (the **GLASS** toggle at the right of the title
  bar) works here and looks right, but it is glass over an opaque window: the
  translucency has nothing behind it to show through.
- **Not distributable.** There is no installer, no icon, no code signature, no
  bundled Python. It runs from a checkout with a built `frontend/dist` and a
  populated `.venv`.
- **Not a second implementation of the service.** It imports and serves
  `service.app.Handler` unchanged. Every route, every response shape, every
  streaming frame is the same code the Cloud Run deployment runs, so nothing
  here can drift from production behaviour.

## Verified

Run end to end on macOS 15 with Chrome present: the app window opened, the
built bundle and its fonts loaded from the loopback port, `/healthz` answered
before the window was launched, closing the window exited the launcher, and
Ctrl-C exited with status 0 leaving no stray browser process and no listener
on the port.

## Showing a run in the open KiCad (`desktop/kicad_live.py`)

The vision for the Kaleo overlay is that the senior engineer reviews in KiCad,
not in the app: the schematic appears, then the placed board, then copper
lands on that same open board, and then that board opens in KiCad's own 3D
viewer. This module is that bridge. It needs KiCad 9 or newer with *Preferences > Plugins >
Enable API server* on, and it runs in its **own** venv because `kicad-python`
pins `protobuf<6` while the Gemini SDK needs `protobuf>=6.33` — the two cannot
share an interpreter, which also guarantees the bridge can never import the
engine.

```sh
python3 -m venv .venv-kicad && ./.venv-kicad/bin/pip install kicad-python kiutils
./.venv-kicad/bin/python desktop/kicad_live.py out/board.kicad_pcb replay   # all four stages
./.venv-kicad/bin/python desktop/kicad_live.py out/board.kicad_pcb routing  # one stage
```

Stages: `schematic` opens `<stem>.kicad_sch` in eeschema; `placement` opens
`<stem>.placed.kicad_pcb` in pcbnew (launched directly — opening a board via
the KiCad app lands in the project manager, which has no board editor behind
its API socket); `routing` creates every track and via from `<stem>.kicad_pcb`
in that open board through the IPC API, idempotently, and leaves it unsaved.
The bridge reaches pcbnew through its per-process socket
(`/tmp/kicad/api-<pid>.sock`), not the shared `api.sock`, which belongs to
whichever editor started first — the schematic editor, in this flow, and it
has no board handler;
`3d` opens the routed `<stem>.kicad_pcb` in the board editor and then asks
that editor for its 3D viewer -- over the IPC API's `RunAction` where the API
server is on, falling back to clicking the menu with AppleScript. The board is
opened first because the viewer is a *window of the board editor* and can only
ever show what that editor has open. There is no `case` stage: the enclosure is
a STEP assembly handed to whatever owns `.step`, and nothing here launches a CAD
GUI (the OpenSCAD window went with the v1 emitter on 2026-09-08). Nothing here
writes a file; saving the board is the engineer's decision.
