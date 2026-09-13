# Native shell: implementation and roadmap

`desktop/silkscreen-app` is a browser window in disguise, and it hits a hard
ceiling: Chrome's `--app` mode gives no control over window level,
transparency, or global shortcuts. Those three are exactly what a Cluely-style
ambient overlay *is*. This document records both the native foundation now in
the repository and the remaining overlay and distribution work.

The implemented foundation is a fresh MIT-licensed Tauri 2 host in
`desktop/src-tauri/`. It embeds the existing Svelte bundle, owns a loopback
Python sidecar, routes HTTP through Tauri's native HTTP plugin, uses a native
Save dialog, and toggles its normal window with
`CommandOrControl+Shift+K`. Rust tests, Clippy, the frontend suite, and a live
macOS launch have been verified. Transparent overlay behavior, bundled Python,
signing, notarization, and `.dmg` publishing remain roadmap items.

---

## 1. Version and project shape

**Tauri 2.x** (latest core release 2.11.5, 2026-07-01; there is no v3).
<https://tauri.app/release/core/>

The shell was added to the existing tree rather than introducing another UI:

```sh
npx tauri init      # or: cargo tauri init
```

The resulting `desktop/src-tauri/` contains `Cargo.toml`, `tauri.conf.json`,
and `capabilities/default.json`, so the repo root remains a Python project.
<https://v2.tauri.app/start/create-project/>

### Loading `frontend/dist`

The SPA is already a plain static bundle with no server-side rendering and no
API base URL baked in, so Tauri loads it directly:

```json
{
  "build": {
    "frontendDist": "../../frontend/dist",
    "beforeBuildCommand": "npm --prefix ../../frontend run build"
  }
}
```

- `frontendDist` — "The path to the application assets (usually the `dist`
  folder of your javascript bundler)". In a release build Tauri embeds those
  files in the binary and serves them over the `tauri://` custom protocol.
- `devUrl` and `beforeDevCommand` are intentionally omitted. The documented
  `cargo run` path does not invoke Tauri CLI hooks, so configuring a Vite URL
  there would open a blank webview unless somebody separately started Vite.
  Development and debug builds load the already-built `frontendDist` instead.
<https://v2.tauri.app/reference/config/>

**The consequence that matters:** in a built shell the page origin is
`tauri://localhost`, not `http://127.0.0.1:PORT`. `frontend/src/lib/api.js`
fetches same-origin relative paths, so under Tauri those resolve against
`tauri://` and 404. The implemented shell injects the sidecar origin as
`globalThis.__SILKSCREEN_BASE__`; `frontend/src/lib/transport.js` keeps browser
requests relative and sends desktop requests through `@tauri-apps/plugin-http`.
The Rust parser and Tauri capability both restrict that bridge to canonical
`http://127.0.0.1:<port>` origins.

---

## 2. The Python backend: two options

The engine cannot be ported. It is OR-Tools CP-SAT and `kiutils`; there is no
Rust equivalent of the CP-SAT model in `packing.py` and rewriting it would
throw away the invariant that the whole test suite guards. So Python ships, or
Python is required. Those are the only two shapes.

### Option A — PyInstaller sidecar (a real installable app)

Tauri's sidecar mechanism embeds an arbitrary executable in the bundle.
<https://v2.tauri.app/develop/sidecar/>

```json
{ "bundle": { "externalBin": ["binaries/silkscreen-service"] } }
```

Tauri looks for `binary-name{-target-triple}{.system-extension}`, so on disk
the files must be named:

```
binaries/silkscreen-service-aarch64-apple-darwin
binaries/silkscreen-service-x86_64-apple-darwin
binaries/silkscreen-service-x86_64-pc-windows-msvc.exe
binaries/silkscreen-service-x86_64-unknown-linux-gnu
```

Get the host triple with `rustc --print host-tuple`.

Permission, in `capabilities/default.json` — `shell:default` is **not**
enough, it only grants `allow-open` for `http(s):`/`tel:`/`mailto:`:

```json
{ "permissions": [
  { "identifier": "shell:allow-spawn",
    "allow": [{ "name": "binaries/silkscreen-service", "sidecar": true }] }
]}
```

Spawn it from Rust in `setup`:

```rust
use tauri_plugin_shell::ShellExt;

let (mut rx, _child) = app.shell()
    .sidecar("silkscreen-service")?      // filename only, no "binaries/" prefix
    .env("PORT", port.to_string())
    .spawn()?;
```

Note the asymmetry: the Rust `sidecar()` takes the bare filename, while the JS
`Command.sidecar()` from `@tauri-apps/plugin-shell` takes the exact
`externalBin` string. Mixing them up is the standard first bug.

**Port handshake.** Do not hardcode a port; a desktop app that dies because
another process owns 8080 is not an app. Two workable shapes:

- Rust binds `TcpListener::bind("127.0.0.1:0")`, reads the assigned port, drops
  the listener, and passes it as `PORT`. Small TOCTOU window, in practice fine.
- Better: the sidecar binds port 0 itself and prints the real port on stdout;
  Rust reads it off the `CommandEvent::Stdout` stream before creating the
  window. `desktop/launcher.py:start_server` is already exactly that entry
  point — it binds `("127.0.0.1", 0)` and returns the server, whose
  `server_port` is the answer. The sidecar entry script is `launcher.py` with
  the browser-opening half removed.

Never point the sidecar at `service.app.make_server`: it binds `0.0.0.0`, which
in a shipped desktop app publishes an unauthenticated `/generate` to the local
network.

**Build step**, run before `tauri build`:

```sh
pyinstaller --onedir --name silkscreen-service \
    --collect-all ortools --collect-all kiutils --collect-all google \
    desktop/sidecar_entry.py
```

`--collect-all ortools` is not optional: OR-Tools is a native extension with
`.so`/`.dylib` payloads and protobuf data files that PyInstaller's static
analysis does not find on its own, and the failure mode is an app that builds
cleanly and then raises `ImportError` on the first solve.

### Option B — require a local Python (a wrapper, honestly labelled)

Skip `externalBin`. Use Rust's `std::process::Command` to run the dedicated
`desktop.sidecar` module from a path the app discovers or the user configures,
exactly as `silkscreen-app` discovers its local Python today.

- **Pro:** no 40 MB per-platform payload, no notarization of a Python binary,
  no antivirus reputation problem, and the app tracks the checkout — which is
  what a developer tool actually wants during a hackathon.
- **Con:** it is not something you hand to somebody who does not have the
  repo. It is `silkscreen-app` with a nicer window.

**Current implementation:** Option B is complete for development. Rust starts
`.venv/bin/python -m desktop.sidecar`, reads one JSON readiness record, keeps
the child's stdin open as an ownership signal, and shuts it down on app exit.
Move to Option A before enabling Tauri bundles or publishing a `.dmg`.

---

## 3. Window configuration for the overlay

```json
{
  "app": {
    "macOSPrivateApi": true,
    "windows": [{
      "label": "overlay",
      "title": "silkscreen",
      "width": 520, "height": 720,
      "alwaysOnTop": true,
      "transparent": true,
      "decorations": false,
      "shadow": false,
      "skipTaskbar": true,
      "visibleOnAllWorkspaces": true,
      "resizable": true,
      "focus": false
    }]
  }
}
```

All of these are documented `app.windows[]` keys.
<https://v2.tauri.app/reference/config/>

- `transparent` — the window background is not painted, so whatever the CSS
  leaves transparent shows the desktop through it.
- `decorations: false` — no OS title bar. The SPA already draws its own
  (`TitleBar.svelte`), so this is a straight swap rather than a loss; the
  shell adds `data-tauri-drag-region` to that element's container so the app's
  own bar drags the window.
- `alwaysOnTop` — the overlay stays above other apps.
- `visibleOnAllWorkspaces` — macOS/Linux only; `set_visible_on_all_workspaces`
  is documented "Windows / iOS / Android: Unsupported".
- `shadow: false` — on Linux `set_shadow` is unsupported outright; on Windows
  `true` on an undecorated window adds a 1 px white border, which reads as a
  rendering bug against a dark glass panel.

Runtime counterparts, for the hotkey handler and the click-through toggle
(`@tauri-apps/api/window`, and `tauri::window::Window` in Rust):

- `setAlwaysOnTop(bool)` / `set_always_on_top(bool)`
- `setIgnoreCursorEvents(bool)` / `set_ignore_cursor_events(bool)` — "Changes
  the cursor events behavior". This is the click-through that makes an overlay
  ambient rather than obstructive: mouse events pass to the app underneath
  while the panel stays visible.
- `setDecorations(bool)`, `setVisibleOnAllWorkspaces(bool)`
<https://v2.tauri.app/reference/javascript/api/namespacewindow/>

### The macOS transparency tax

Transparent windows on macOS require **both** the `macos-private-api` Cargo
feature on the `tauri` crate and `"macOSPrivateApi": true` in the config;
setting one without the other is a build error (tauri#11142). The
consequence is not cosmetic: **the app cannot go to the Mac App Store.**
Developer-ID signing and notarization for direct distribution still work.
<https://github.com/tauri-apps/tauri-docs/issues/463>

### Real material, not just alpha

For the frosted-glass look to be *material* rather than a flat tint, add the
`window-vibrancy` crate (tauri-apps org):

```rust
use window_vibrancy::{apply_vibrancy, NSVisualEffectMaterial};
apply_vibrancy(&window, NSVisualEffectMaterial::HudWindow, None, None)?;
```

macOS 10.10+ gets `apply_vibrancy` (NSVisualEffectView); macOS 26+ adds
`apply_liquid_glass`; Windows gets `apply_blur` / `apply_acrylic` /
`apply_mica`. **Linux is unsupported** — blur there is the compositor's
decision, not the app's. It requires `transparent: true`, `macOSPrivateApi:
true`, and a transparent `html, body` background.
<https://github.com/tauri-apps/window-vibrancy>

---

## 4. Turning on the glass skin

The SPA is already built for this. `frontend/src/styles/glass.css` is a
token-override block keyed on `:root[data-skin='glass']`, and
`frontend/src/main.js` sets that attribute at boot from
`localStorage['silkscreen-skin']` (`frontend/src/lib/skin.js`, `STORAGE_KEY =
'silkscreen-skin'`, values `'glass'` / `'paper'`). Every panel already carries
`backdrop-filter: var(--glass-blur, none)`, inert under the default skin.

So the shell does not need a build flag or a frontend rewrite. It needs an
**initialization script**, which Tauri runs before any page script on every
navigation:

```rust
tauri::WebviewWindowBuilder::new(app, "overlay", WebviewUrl::default())
    .initialization_script(r#"
      // The stored choice wins: the title-bar GLASS toggle is the user's, and
      // a shell that overwrote it every launch would make the toggle look
      // broken. Seed the default only when nothing is stored.
      try {
        if (!localStorage.getItem('silkscreen-skin'))
          localStorage.setItem('silkscreen-skin', 'glass');
      } catch (e) {}

      // The window is transparent, so the page must not paint an opaque
      // ground behind the panels; glass.css sets --paper and a body gradient
      // for a browser window, which has nothing behind it to show.
      addEventListener('DOMContentLoaded', () => {
        const s = document.createElement('style');
        s.textContent =
          'html,body{background:transparent!important;background-image:none!important}';
        document.head.appendChild(s);
      });

      window.__SILKSCREEN_BASE__ = 'http://127.0.0.1:__PORT__';
    "#)
    .transparent(true)
    .decorations(false)
    .always_on_top(true)
    .build()?;
```

Three things in one place, and each is doing something the CSS cannot:
selecting the skin without touching the build, clearing the ground the skin
deliberately paints for a browser window, and handing over the sidecar's port.

**Does `backdrop-filter` survive the webview?** On macOS WKWebView and on
Windows WebView2 (Chromium), yes. On Linux WebKitGTK it is implemented
(WebKit bug 169988, fixed 2020) but it **blurs page content only — it does not
blur the desktop behind a transparent window**, and combining `transparent:
true` with `backdrop-filter` misbehaves outright (tauri#12804, #6876, #2827).
Plan for the Linux build to look like a flat translucent tint, and do not
treat that as a bug to chase.

---

## 5. Global hotkey

Crate `tauri-plugin-global-shortcut`, npm `@tauri-apps/plugin-global-shortcut`.
Desktop-only, so gate the dependency:

```sh
cargo add tauri-plugin-global-shortcut \
  --target 'cfg(any(target_os = "macos", windows, target_os = "linux"))'
```

```rust
app.handle().plugin(tauri_plugin_global_shortcut::Builder::new().build())?;
app.global_shortcut().register("CommandOrControl+Shift+K")?;
```

Permissions `global-shortcut:allow-register`, `...:allow-unregister`, and
`...:allow-is-registered` are needed only when JavaScript owns registration.
The current handler is Rust-side, so those commands are not exposed to the
webview.
<https://v2.tauri.app/plugin/global-shortcut/>

The implemented Rust handler toggles `window.show()`/`hide()` plus
`set_focus()`. A shortcut conflict is logged without aborting startup, so the
normal desktop window still works when another app owns that key combination.

---

## 6. Known problems, by option

| Problem | Where it bites | Mitigation |
| --- | --- | --- |
| **PyInstaller payload** | Option A. A Python server sidecar lands around 35–40 MB per platform, once per target triple. OR-Tools makes this repo's worse than typical. | Accept it, or Option B. |
| **macOS notarization of the sidecar** | Option A. Notarization succeeds with `externalBin` removed and fails with it present (tauri#11992): the notary service requires every nested binary signed with Hardened Runtime — and PyInstaller output *crashes* under Hardened Runtime without `com.apple.security.cs.allow-unsigned-executable-memory` (pyinstaller#4629). Tauri's signing docs do not mention sidecar entitlements at all. | Sign the sidecar yourself with an entitlements plist **before** `tauri build`, and set `bundle.macOS.entitlements`. Budget a day for this specifically. |
| **Windows SmartScreen / AV** | Option A. `--onefile` PyInstaller output self-extracts to `%TEMP%`, which matches packer heuristics and gets flagged (pyinstaller#6754). | Use `--onedir`, code-sign (an EV cert clears SmartScreen reputation fastest), set icon and version metadata, submit false positives to Microsoft. |
| **No Mac App Store** | Both options, if you want transparency. `macos-private-api` disqualifies the bundle. | Developer-ID + notarization, direct download. Decide this before designing a store listing. |
| **Linux blur** | Both options. WebKitGTK does not blur behind a transparent window; compositor blur is not the app's to request. | Ship the flat-tint reading and say so. |
| **`tauri://` origin breaks same-origin fetch** | Resolved in the foundation. | Keep the strict `__SILKSCREEN_BASE__` seam and native HTTP transport tests. |
| **Sidecar orphaning** | Both options. If the shell is force-killed the Python process can outlive it and keep the port. | Hold the child and stop it on `RunEvent::ExitRequested`; the implemented sidecar also exits when its stdin closes. |
| **First-launch cost** | Both options. Importing OR-Tools takes seconds; a transparent always-on-top window that shows nothing for four seconds looks broken. | The implemented host creates its window only after `/healthz` answers. Add a splash if packaged startup is still visibly slow. |
| **Developer-only sidecar** | The current Option B host resolves a checkout and local Python environment. | Keep `bundle.active` false. Implement Option A, signing, and notarization before publishing installers. |

---

## 7. Suggested order

1. **Done:** native host under `desktop/src-tauri/`, `frontendDist` at
   `frontend/dist`, and the parent-owned Option B backend in a normal window.
2. **Done:** strict `__SILKSCREEN_BASE__` transport, native Save dialog, global
   summon/dismiss shortcut, and macOS CI for Rust plus the shared frontend.
3. **Next:** turn on `transparent` + `decorations: false` + `alwaysOnTop` +
   `macOSPrivateApi`, add the initialization script, confirm the glass skin
   reads correctly against a live desktop.
4. Add `window-vibrancy` for real material and `setIgnoreCursorEvents` for
   click-through.
5. Only then implement Option A, PyInstaller, and signing — and budget the macOS
   entitlements problem as its own task, not as a build-config tweak.
