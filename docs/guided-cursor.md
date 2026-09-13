# The guided cursor: what MudrikNow does, and what we would build

Feature 7 in DEVPOST ("Show, don't tell", `DEVPOST.md:128-133`) is the on-screen
pointer that lands on the control a person should click next. This note reads the
vendored reference, separates the parts that transfer from the parts that do not,
and proposes an architecture against the Tauri shell in `app/`.

**Verdict up front.** `CLAUDE.md`'s claim — that `vendor/mudriknow/` is a working
reference for exactly this feature — is *true about the design and false about the
code*. MudrikNow really does point at arbitrary other applications, not just its
own UI, and its state machine is the best free specification of this feature I have
seen. But every line that touches the operating system is Windows-only by the
authors' own statement (`vendor/mudriknow/AGENTS.md:57`: "Windows-only,
end-to-end… Do not add `process.platform` branches unless you are also porting the
PowerShell layer"), and it is Electron, not Tauri. We would be reimplementing the
design, not porting the code. Budget accordingly.

**Second verdict, added 2026-09-06 after the first build.** That prediction held:
step one of §5 shipped as a fresh Svelte implementation with none of MudrikNow's
code in it. So the feature is now **half built**, and the two halves should never
be quoted as one status — §3(a), the pointer inside our own webview, is done and
in the shipped bundle; §3(b), pointing at KiCad or anything else on screen, is not
started, and the piece of our own tree this document cited as its head start
(`app/src-tauri/src/capture.rs`) has since been deleted, which makes (b) *more*
expensive than estimated below, not less.

---

## 1. What MudrikNow actually does

Auto-Guide is a **teaching** mode, explicitly not an automation mode: the app
points, the *human* clicks. `vendor/mudriknow/src/main/action-executor.ts:73-77`
states the distinction in the comment on the dispatcher — guide markers are gated
by `autoGuideEnabled`, **not** by `actionsEnabled`, "the guide doesn't drive the
desktop directly; it just shows the user where to click." That is our feature
almost exactly.

### 1.1 The loop

Model output is plain prose with embedded HTML-comment markers. `parseActionsFromResponse`
(`src/main/action-executor.ts:285-330`) scans the streamed text for
`<!--ACTION:{…}-->`, JSON-parses each one, and — for the four guide types — passes
the whole parsed object through untouched to the controller
(`action-executor.ts:322-327`). The four markers are `guide_offer`, `guide_step`,
`guide_complete`, `guide_abort` (`src/shared/types.ts:43-46, 124-126`).

`GuideController` (`src/main/guide/guide-controller.ts`) is a state machine over
`idle → offer → step-active → waiting → recapturing → awaiting-ai`
(`guide-controller.ts:38-45`). The interesting parts:

- **The offer is a consent gate.** The model must emit `guide_offer` first; an
  out-of-band `guide_step` throws rather than pointing (`guide-controller.ts:380-386`).
- **The first step rides along with the offer** and is held locally
  (`deferredFirstStep`, `guide-controller.ts:165-173`, executed at `:230-236`), so
  tapping "Start guide" points immediately instead of costing a model round-trip.
- **Advancing is user-confirmed, not observed.** `advanceFromStep`
  (`guide-controller.ts:542-...`) waits `waitMs`, hides the panel, recaptures the
  screen, and sends a follow-up. Crucially, click detection is **off**: the block
  at `guide-controller.ts:476-489` is a commented-out `WH_MOUSE_LL` global mouse
  hook with a note explaining why it was disabled — it raced with the user's own
  button taps and mis-scoped to the panel's window. The shipping design is "the
  owl points, you click in your app, you tap *I did it*."
- **Failure mode is designed.** If no bounds can be resolved, the code shows no
  pointer and relies on the caption; the prompt says it outright
  (`src/shared/prompts.ts:367`: "An off-by-50px owl is worse than no owl").
- Five-minute inactivity abort (`guide-controller.ts:135, 597-606`), a
  `runGeneration` counter so a cancel invalidates an in-flight pipeline
  (`:285-291, 552-553`), and `closeOptions` to end the guide without burning a
  round-trip on "yes I'm done" (`:254-260`).

### 1.2 How it decides *where* to point

Two independent coordinate sources, fused by the model, not by code:

1. **A UI Automation tree of the active window**, produced by a large embedded
   PowerShell script (`src/main/context-reader.ts:9` —
   `hoverbuddy-read-context-v31-chromium-screenshot.ps1`; the script is assembled
   line-by-line from `getScriptContent()` at `:10` onward). It P/Invokes `user32`
   and drives `System.Windows.Automation`. There is a genuinely clever bit at
   `context-reader.ts:53-77`: Chromium/Electron apps keep their UIA tree empty
   until an assistive client announces itself, so the script sends `WM_GETOBJECT`
   with `UiaRootObjectId` *and* registers a no-op UIA focus handler to trip
   Chromium's `UiaClientsAreListening()` flag. Up to 50 clickable candidates —
   name, `automationId`, real pixel bounds — go into the prompt
   (`src/main/ipc-handlers.ts:675, 681`).
2. **A full-screen screenshot with a numbered coordinate grid burned into it**,
   also via PowerShell + GDI+ (`src/main/vision.ts:60-95`): ~25 columns and rows,
   faint grid lines, numbered cells, downscaled to 1280px longest side. The prompt
   tells the model the cell dimensions and the physical screen size and asks it to
   *count cells* (`ipc-handlers.ts:681`).

The model then returns **exactly one** of two bounds fields (`prompts.ts:344-366`,
`types.ts:72-105`):

- `uiaBounds` — copied verbatim from the candidate list. Pixel-perfect.
- `guessBounds` — estimated from the grid. The fallback for web/Chromium/canvas
  content where UIA is blind.
- or `target: null`, meaning caption-only, no pointer.

The runtime's resolution order is `uiaBounds → guessBounds → legacy boundsHint →
live UIA lookup → nothing` (`guide-controller.ts:420-464`). Note the honesty of
the design: `prompts.ts:249` and `:356` tell the model the *screenshot grid*, not
the accessibility tree, is the single source of truth for coordinates, because
"UIA is blind to web/Chromium content and its container bounds can be wildly off."
That is a hard-won finding and it will be true for us too.

### 1.3 How it renders the pointer

An **OS-level overlay window**, not a DOM injection and not a screenshot
annotation. `src/main/guide/guide-overlay.ts:86-127` creates one Electron
`BrowserWindow` spanning the **union of all display bounds** (`:89-93`) with
`frame: false, transparent: true, alwaysOnTop: true, focusable: false,
skipTaskbar: true`, loading a local HTML file
(`src/main/guide/guide-overlay.html`, 343 lines of CSS/markup) that draws an owl
sprite and a speech bubble. On Windows it is raised to
`setAlwaysOnTop(true, "screen-saver")` (`guide-overlay.ts:119-124`) because plain
always-on-top loses to context menus and other topmost windows.

Two details worth stealing:

- **Click-through is polled, not event-driven.** The window is
  `setIgnoreMouseEvents(true, { forward: true })` by default
  (`guide-overlay.ts:118`); a 30 ms timer reads `screen.getCursorScreenPoint()`
  and flips hit-testing on only while the cursor is inside the reported owl or
  bubble rectangles (`guide-overlay.ts:30-84`). The comment at `:30-38` says
  forwarded mouse-move was unreliable when another window sat underneath, leaving
  the bubble visually on top while clicks fell through. Expect the same class of
  bug.
- **DPI is handled explicitly and painfully.** All bounds cross the boundary in
  *physical* pixels and are converted to logical/DIP against the scale factor of
  the display that actually contains the point
  (`guide-overlay.ts:129-145, 153-172`), because a multi-monitor mixed-DPI setup
  has no single scale factor. `boundsHintToPhysical` (`guide-controller.ts:22-36`)
  is the inverse for the legacy path.

There is also a simpler sibling: `src/main/highlight.ts:5-85` spawns a tiny
transparent click-through window sized to a single element's bounds and animates a
glowing border for 2 seconds. That is the cheapest possible version of this
feature and a good model for our first step (§5).

To move the target app back to the foreground before recapturing, MudrikNow uses
`koffi` FFI into `user32.dll` — `GetForegroundWindow`, `SetForegroundWindow`,
`AttachThreadInput`, `keybd_event` (`src/main/guide/active-window.ts:16-34,
93-138`). The `AttachThreadInput` dance at `:105-137` exists because Windows
refuses foreground changes from a process without recent input.

### 1.4 What it needs to know about the target app

Nothing, structurally — and that is the point. It needs (a) the foreground window
handle, (b) an accessibility tree for that handle, (c) a screenshot, (d) a model
that can read both. There is no per-application adapter, no KiCad plugin, no
selector database. Robustness comes entirely from the accessibility tree being
good, and the grid screenshot is the confession that it often is not.

---

## 2. What transfers, and what does not

**It genuinely points at other applications.** This is the load-bearing question
and the answer is yes: the overlay covers the whole virtual desktop, coordinates
are absolute screen pixels derived from *another process's* UIA tree, and
`prompts.ts:296-300` scopes the guide to "THE APP THAT IS RIGHT IN FRONT OF THEM."
So MudrikNow is a reference for architecture (b) in §3, the hard one, not the easy
one. `CLAUDE.md`'s framing is correct on that point.

**What transfers:**

| Transfers | Why |
| --- | --- |
| The four-marker protocol and the offer→step→complete state machine | Pure logic, no OS calls. `guide-controller.ts` is ~640 lines of directly reusable *design*. |
| Dual-bounds (`uiaBounds` xor `guessBounds` xor `null`) | The single best idea in the codebase. Accessibility-first, vision-fallback, silence when unsure. |
| The grid-annotated screenshot as a coordinate ruler | Model-side, platform-independent. Reproducible with any capture API. |
| User-confirmed advancement instead of click observation | Learned the hard way (`guide-controller.ts:476-489`); saves us a global mouse hook and its permission burden. |
| "No pointer beats a wrong pointer" | A product rule, and the right one. |
| The transient-UI warning (`prompts.ts:381-405`) | Every click on our panel closes the menu the user just opened. This is OS-level and will bite us identically on macOS. |

**What does not transfer:**

- **Everything platform-facing.** PowerShell + `System.Windows.Automation`
  (`context-reader.ts`), GDI+ capture (`vision.ts`), `koffi`/`user32.dll`
  (`active-window.ts`), `robotjs` (`package.json:40`). Their own docs say
  Windows-only end-to-end (`AGENTS.md:57`).
- **Electron.** `BrowserWindow`, `setIgnoreMouseEvents`, `ipcMain`,
  `screen.getCursorScreenPoint`. Tauri has analogues for some of these, not all
  (§4).
- **Windows has no permission gate for any of this.** macOS does, and it changes
  the product (§3).
- **The `"screen-saver"` always-on-top level** (`guide-overlay.ts:122-124`). I do
  not know whether Tauri 2 exposes a window-level equivalent; treat as unverified.
- **Their assumption that the guided app is a normal native app.** KiCad on macOS
  is wxWidgets over Cocoa. Whether its `AXUIElement` tree exposes toolbar buttons
  and menu items with usable names and frames is **unverified** — nobody in this
  repo has run an accessibility inspector against KiCad. That single unknown
  decides whether we get `uiaBounds`-quality pointing or are permanently in
  `guessBounds` territory. **Check this before committing to architecture (b).**

**Licensing.** MudrikNow is MIT; `app/` is GPL-3 (`app/NOTICE.md`). MIT code may
be pulled into `app/` (it becomes GPL-3 there, with MIT attribution preserved) —
but not the reverse, and nothing from `app/` may move into `engine/`, `service/`,
or `frontend/`. Since the overlay must live in the Tauri shell, any
MudrikNow-derived code lands on the GPL side. That is legal and one-way; keep the
copyright header on anything actually copied, and prefer reimplementing from this
note over pasting.

---

## 3. The two architectures

### (a) Point only inside our own webview

The pointer is a DOM element in the SPA. Targets are `data-testid` attributes —
which we already have as a documented convention, on intrinsic elements, with
`data-ref` / `data-sev` for disambiguation. `getBoundingClientRect()` gives exact
bounds, in one coordinate frame, with no DPI conversion, no capture, no
permissions, and no risk of pointing at nothing.

- **Cost:** low. A few hundred lines of Svelte. No Rust.
- **Ceiling:** it can only teach *our* UI. "Click the net class button in KiCad"
  is out of scope forever.
- **Honest value:** non-zero but modest. Our SPA is one column of findings and a
  board well; it is not KiCad-dense. The feature's pitch is about EDA-tool
  density, and we would be pointing at our own five buttons.

### (b) Point at arbitrary on-screen targets

The MudrikNow architecture, ported. Four pieces:

1. **A transparent, click-through, always-on-top overlay window** spanning the
   virtual desktop, or one per monitor. **This no longer has a precedent in our
   tree.** Earlier revisions of this document cited
   `app/src-tauri/src/capture.rs:95-158` as a working multi-monitor overlay
   builder; that file was **deleted in `376ee57`** ("Remove the legacy Pluely
   product from Kaleo") and nothing replaced it. Budget this as new work.
2. **Screen capture**, to give the model something to look at. Also new work:
   `xcap` is **not** in `app/src-tauri/Cargo.toml` any more, and no
   `Monitor::all()` call survives. Adding it means adding a dependency, a capture
   path, and the macOS Screen Recording permission prompt that comes with it —
   plus the numbered grid at capture time, as `vision.ts:60-95` does.
3. **A coordinate source.** Either accessibility APIs — macOS `AXUIElement`
   (`AXUIElementCopyAttributeValue`, `kAXPositionAttribute` / `kAXSizeAttribute`),
   Windows `IUIAutomation` — or model-estimated grid coordinates, or both fused as
   MudrikNow does. I believe Rust crates exist wrapping both
   (`accessibility-sys`, `uiautomation`); I have not verified their APIs and have
   not built anything here, so treat crate names as leads, not facts.
4. **The marker protocol and state machine** from §1.

**macOS permissions — the part that cannot be engineered around.**

- **Screen Recording** (TCC `kTCCServiceScreenCapture`) is required to capture any
  window content other than our own. Without it, modern macOS returns desktop
  wallpaper instead of an error, so the failure looks like a broken model rather
  than a missing grant. Detect and say so explicitly.
- **Accessibility** (TCC `kTCCServiceAccessibility`) is required to read another
  app's `AXUIElement` tree. `AXIsProcessTrustedWithOptions` with the prompt option
  raises the system dialog; there is no programmatic grant.

Both are **user-granted, per-app, and per-binary**. They cannot be scripted,
entitled, or installer-flagged around; the user opens System Settings → Privacy &
Security and toggles them. They are keyed to the app's code signature, so an
unsigned dev build and a signed release are *different* grants, and re-signing can
silently revoke. Expect to re-grant repeatedly during development, and expect a
first-run onboarding screen to be a real, non-optional piece of work. Windows, by
contrast, needs neither grant — which is exactly why MudrikNow's authors never had
to build that screen.

There is also a plain interaction problem, documented at `prompts.ts:381-405`:
every tap on our panel steals foreground and closes whatever menu the user just
opened, so by the next screenshot the target is gone. MudrikNow's answer is to
instruct the model to re-open menus from scratch each step. There is no better one.

### Recommendation

**Build (a) first as a real, shipped feature; design its internals so (b) is a
back-end swap; do not promise (b) for the hackathon.**

The pointer, the caption bubble, the offer gate, the state machine, and the
prompt-side marker contract are identical in both. The only thing that differs is
where bounds come from and what coordinate frame they arrive in. If the pointer
component takes absolute screen-ish rectangles and a `source: 'dom' | 'screen'`
tag from day one, moving to (b) means adding a bounds provider, not rewriting the
feature.

I would not start (b) before someone runs Accessibility Inspector against KiCad on
this machine and reports whether its toolbar buttons have usable `AXTitle` and
frames. If they do not, (b) degrades to pure vision-model coordinate guessing —
which MudrikNow's own prompt calls the fallback path, and which fails *silently and
confidently*, the worst possible failure for a teaching tool.

---

## 4. Attaching it to the Tauri shell

`desktop/TAURI.md` is prior research and I am not repeating it; it covers
transparency, `macOSPrivateApi`, the global shortcut, the sidecar, and the
`__SILKSCREEN_BASE__` origin seam. Since it was written, the shell has become
`app/` — a GPL-3 fork of Pluely, already Tauri 2, already
`transparent: true, alwaysOnTop: true, decorations: false, skipTaskbar: true,
visibleOnAllWorkspaces: true, macOSPrivateApi: true`
(`app/src-tauri/tauri.conf.json:14-33`, with our deltas logged in
`app/NOTICE.md`). Several of TAURI.md's steps 1–3 are therefore already done by
inheritance. What that document does *not* cover, and what this feature needs:

**The multi-window story was solved in the fork, and then removed.**
`app/src-tauri/src/capture.rs` used to build one `WebviewWindowBuilder` overlay per
monitor, converting physical monitor size and position to logical units by the
per-display `scale_factor()` — the same conversion `guide-overlay.ts:129-172` does,
arrived at independently, and the closest thing we had to a skeleton for the
pointer overlay. **It is gone**: deleted in `376ee57` along with the rest of the
legacy Pluely product. The design note stands (per-display `scale_factor()` is the
conversion you need, and the picker differed from what we want by being
`.focused(true)` and not click-through); the code does not. Recover it from git
history if you want the reference, but plan the overlay as new work.

**Click-through.** `setIgnoreCursorEvents(bool)` / `set_ignore_cursor_events(bool)`
is documented and cited in `desktop/TAURI.md:§3`. It is the analogue of Electron's
`setIgnoreMouseEvents`. **I am unsure whether Tauri's version has an equivalent of
Electron's `{ forward: true }`**, which is what makes hover work through a
click-through window. Given that MudrikNow abandoned forwarding anyway in favour of
polling the cursor position (`guide-overlay.ts:30-84`), the poll approach is
probably the right one for us regardless — and `set_ignore_cursor_events` plus a
cursor poll is enough to reproduce it. Tauri does expose cursor position
(`Window::cursor_position` / the `available_monitors`/`monitor_from_point` family);
I have not verified the exact method names, so confirm against the v2 docs before
writing code.

**Where the pointer lives.** Two options, and I prefer the second:

- A second Tauri webview window labelled `guide-overlay`, loading a dedicated
  route, driven by Tauri events (`emit`/`listen`) from Rust — the direct analogue
  of MudrikNow's `guide-overlay.html` + IPC channel. Full-screen transparent
  webviews are not free, and one per monitor multiplies that.
- **A single small window, moved and resized to hug the target** — the
  `highlight.ts:5-85` model. It is cheaper, avoids full-screen transparency
  entirely, and its animation is confined to a few hundred pixels. Its limits: it
  cannot draw a pointer that *travels* across the screen to the target, and a
  bubble that extends past the target needs the window sized for both. For a first
  version, a hugging halo plus a caption is enough, and DEVPOST's "animated cursor
  across the interface" can come later with the full-screen window.

**What does not exist yet and must be built in Rust:** the accessibility bridge.
Tauri has no built-in plugin for `AXUIElement` or UI Automation, and I know of no
official one. That is new native code per platform, and it is where the schedule
risk concentrates.

---

## 5. Smallest useful first step — **built**

**Point at one control inside our own SPA, driven by a real model marker.**

> **Status 2026-09-06: this step shipped.** `frontend/src/lib/guide.js` holds the
> store, the declarative `{testid, attrs, within}` resolver and the consent gate;
> `frontend/src/components/GuidePointer.svelte` does the measuring and is mounted
> in `App.svelte:317`; `FindingCard.svelte:36` is the "Show me" entry point,
> calling `offerGuide(findingGuideSteps(finding), …)`. The four numbered items
> below are the record of what was specified, and they were followed: targets are
> declarative rather than raw selectors, a step whose target does not resolve
> renders caption-only and says so, advancing is a Next button and never click
> detection, and the pointer re-measures on scroll and resize. Everything under
> "What it explicitly would not do" is still true and still deliberate — nothing
> outside our own window is pointed at, no screen is captured, no accessibility
> tree is read, and the OS cursor is never moved. Section 3(b) above is the part
> that remains unbuilt.

1. Extend the review response so a finding may carry a `guide` marker naming a
   `data-testid` (plus optional `data-ref`), reusing the existing additive-only
   `/generate` rule — no new endpoint, no new permission, no Rust.
2. In `frontend/`, a `GuidePointer` component: given a testid, resolve
   `getBoundingClientRect()`, draw an animated halo plus a caption bubble, and
   fall back to **rendering nothing but the caption** when the element is absent.
   Port that rule verbatim from `prompts.ts:367`.
3. Wire the offer gate: a "Show me" button on a finding → pointer appears with
   caption and a "Got it" / "Cancel" pair → dismiss. That is `guide_offer`,
   `guide_step`, `guide_complete` in miniature, using the same marker names so the
   protocol is already the real one when the bounds source changes.
4. Make the pointer take `{rect, frame: 'dom'}` so a future `frame: 'screen'`
   provider drops in beside it.

Demoable in the browser, in the shipped bundle, on any platform, with no toolchain
this machine lacks.

**What it explicitly would not do:**

- Not point at KiCad, a terminal, a browser, or anything outside our window.
- Not capture the screen; no Screen Recording prompt.
- Not read any accessibility tree; no Accessibility prompt.
- Not move the OS cursor or click anything — the human clicks, always. (Keep this
  property even in (b); it is the difference between a teaching tool and a
  computer-use agent, and it is most of the safety argument.)
- Not survive the user scrolling the panel unless the component re-measures on
  scroll and resize; do that or the halo silently detaches from its target.
- Not require any Rust, and therefore not prove anything about the overlay window,
  the permission flow, or DPI — the three things that will actually be hard.

Being clear about that last point matters: step 1 validates the *protocol and the
interaction*, which is genuinely most of the product design. It validates none of
the platform risk. Do not let a good demo of (a) get reported as progress on (b).

---

## Sources

All paths relative to the repository root.

- `vendor/mudriknow/src/main/guide/guide-controller.ts` — state machine, bounds
  resolution priority (`:420-464`), disabled mouse hook (`:476-489`).
- `vendor/mudriknow/src/main/guide/guide-overlay.ts` — overlay window
  (`:86-127`), click-through poller (`:30-84`), DPI conversion (`:129-172`).
- `vendor/mudriknow/src/main/guide/guide-overlay.html`,
  `guide-overlay-renderer.ts` — pointer and bubble rendering; directional
  placement at `guide-overlay-renderer.ts:59-90`.
- `vendor/mudriknow/src/main/guide/active-window.ts` — `koffi`/`user32` foreground
  handling.
- `vendor/mudriknow/src/main/highlight.ts` — the minimal single-element highlight.
- `vendor/mudriknow/src/main/context-reader.ts` — PowerShell UIA tree; Chromium
  accessibility wake-up at `:53-77`.
- `vendor/mudriknow/src/main/vision.ts:60-95` — grid-annotated screenshot.
- `vendor/mudriknow/src/main/action-executor.ts` — marker parsing (`:285-330`),
  guide dispatch and the actions/guide gating split (`:73-112`).
- `vendor/mudriknow/src/shared/prompts.ts:270-410` — `GUIDE_PROMPT_FULL`.
- `vendor/mudriknow/src/shared/types.ts:40-126` — marker payload shapes.
- `vendor/mudriknow/AGENTS.md:57` — the Windows-only statement.
- `app/src-tauri/tauri.conf.json`, `app/src-tauri/Cargo.toml` — the shell as it
  stands. (`app/src-tauri/src/capture.rs`, cited in earlier revisions, was deleted
  in `376ee57`; read it in git history, not in the tree.)
- `app/NOTICE.md` — the GPL-3 boundary.
- `desktop/TAURI.md` — prior Tauri research.
