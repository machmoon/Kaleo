# Overlay motion audit — what the Kaleo bar does today

Read-only inventory taken 2026-09-06 on branch `local/openscad`, ahead of the
expand/collapse rebuild. Every claim below is cited to `file:line` in the tree
as it stands; one defect was fixed and is recorded in §6 with a before/after.

The thing to hold in your head while reading: **the overlay window is a native
600×58 Tauri panel with `resizable: false`**
(`app/src-tauri/tauri.conf.json:15-31`), and its size is driven entirely from a
`ResizeObserver` on one DOM node
(`app/src/hooks/useOverlayHeight.ts:58-102`). There is no CSS transition on the
card's height anywhere (§2.3). So every state change in this component is a
*native window resize*, applied in one step, on a frame the browser has already
painted at the wrong size. That is the whole of the current motion design.

---

## 1. State table

### 1.1 How the two top-level shapes are chosen

`app/src/pages/kaleo/index.tsx:146-151` computes `open` from
`isOverlayExpanded` (`app/src/lib/overlay-mode.ts:26-42`). The four reasons, in
precedence order, are `busy` → `stepsActive` → `status !== "idle"` → `expanded`.
`compact` handed to the height hook is exactly `!open`
(`app/src/pages/kaleo/index.tsx:179`). The two shapes are two *sibling JSX
branches* (`index.tsx:499-522` pill, `index.tsx:523-743` bar), so switching
between them unmounts one subtree and mounts the other.

### 1.2 Height arithmetic used below

* `Button size="icon"` = `size-9` = **36 px**; `size="sm"` = `h-8` = **32 px**
  (`app/src/components/ui/button.tsx:23-28`).
* `Input` = `h-9` = **36 px** (`app/src/components/ui/input.tsx:11`).
* Full-bar `Card` is `w-full flex flex-col gap-2 p-2` (`index.tsx:524`), so
  `p-2` = 8 px each side and `gap-2` = 8 px between blocks; `Card` itself adds a
  1 px border (`app/src/components/ui/card.tsx:10`; the base `py-6 gap-6` is
  overridden by tailwind-merge).
* `text-[11px]` is an arbitrary font-size only, so it inherits line-height 1.5 →
  **16.5 px**; with `leading-tight` → **13.75 px**. `text-xs` = 12/16 px.
  `text-[10px]` → **15 px**.
* **Base full bar** = 8 + 36 + 8 + 2 = **54 px** of content, clamped up to the
  58 px floor (`useOverlayHeight.ts:20,29-37`). The 4 px of slack *is* the
  retina-rounding headroom the constant's comment describes
  (`useOverlayHeight.ts:16-20`).
* Every additional block below therefore costs **8 px (gap) + its own height**.

Numbers marked "derived" are computed from the classes, not measured on screen;
treat them as ±4 px and confirm against the running app before hard-coding.

### 1.3 The states

| # | State | Entered by | Left by | Renders | Height @600 px |
|---|---|---|---|---|---|
| 1 | **Idle pill** | `expanded=false` and nothing running (`overlay-mode.ts:26-38`); reached via the collapse arrow `index.tsx:531` | `setExpanded(true)` from the arrow (`CompactBar.tsx:93`), a transcript (`index.tsx:509-518`), the `focus-text-input` event (`index.tsx:161-164`), a spoken command/caption (`index.tsx:342,351`), or any of busy/steps/result | `CompactBar` inside a `w-fit` Card `px-1.5 py-1` (`index.tsx:500-520`) | **content 46 → clamped to 58.** Derived: 4+36+4+2. Width is the interesting axis: **≈132 px** (orb 18 + mic 36 + chevron 36 + drag ≈16, three `gap-1`s, `px-1.5`, borders), floor 120 (`useOverlayHeight.ts:24,45-49`) |
| 2 | **Idle pill, listening** | `barContent()` returns `listening` — mic actually open or TTS speaking (`overlay-mode.ts:76-79`, `CompactBar.tsx:56-62`) | mic closes and TTS stops | Same Card; the chevron is *replaced* by a `max-w-44 truncate` status span (`CompactBar.tsx:79-99`) | Height unchanged (58). **Width jumps to ≈280 px** — `max-w-44` = 176 px replacing a 36 px button. This is a native resize with no height change |
| 3 | **Full bar, idle** | `expanded=true`, everything else idle | collapse arrow (enabled only here — `index.tsx:533`) | Row only: collapse ‹, `PromptBar`, dashboard, `DragButton` (`index.tsx:525-577`) | **54 → 58** |
| 4 | **Full bar, listening** | as #2 but in the open bar | as #2 | `ListeningPanel` swapped in for the `Input`, deliberately the same `h-9` (`PromptBar.tsx:99-133,253-274`) | **58**, unchanged — this swap is already height-neutral by design and is the model the rebuild should follow |
| 5 | **Desk caption** | `action.kind === "caption"` from the wake path (`index.tsx:349-357`) | never dismissed on its own; `deskCaption` has no clear path | `<p data-testid="desk-caption">` (`index.tsx:579-587`) | +8 + **14 px/line**; content-dependent, 1–3 lines typical → **72–100 total** |
| 6 | **Engine unreachable** | `engineDown` = settled probe, not ok, not busy (`index.tsx:486`) | a successful re-probe | destructive banner with a `sm` Retry button (`index.tsx:589-614`) | +8 + **44** (32 button + `py-1.5`) → **110**. Text can wrap past the button at long `baseUrl`s |
| 7 | **Ada caption / command note** | `setCommandNote` from `interpret`, the visibility guard, the arm decay (`index.tsx:203,240-248,444-446`) | Dismiss (`index.tsx:627`) or a `stepsActive` flip (it is hidden, not cleared, at `index.tsx:616`) | muted banner + Dismiss (`index.tsx:616-633`) | +8 + **45–62** — the visibility-guard sentence is long enough to wrap to 2–3 lines at 11 px |
| 8 | **Run in progress** (one-shot) | `busy && !stepsActive` (`index.tsx:663`) | run settles | `RunProgress` + activity disclosure (`index.tsx:663-689`, `RunProgress.tsx:44-127`) | +8 + **≈198** with the feed closed. Derived: border+`pt-2` 9, header 32, gap 8, 7 stage rows (`src/lib/silkscreen/stages.ts`, 7 descriptors) at 16 + 6×2 = 124, gap 8, disclosure 17. **→ ≈260 total** |
| 9 | **Run in progress, "no events yet"** | no stage has ticked (`RunProgress.tsx:45,117-124`) | first stage frame | adds a 2-line paragraph | **+≈33 → ≈293** |
| 10 | **Activity feed open** | the disclosure button (`index.tsx:672-684`) | same button | `ActivityFeed` capped `max-h-40` (`index.tsx:686`) | **+8 +160 → ≈461**, and it stops growing there — the only pre-existing clamp in the busy path |
| 11 | **Result** | `run.status === "done" && run.result` (`index.tsx:691`) | `run.reset()` (`index.tsx:702`) | `RunSummary` + `VoiceToggle` + `SaveBoardButton` (`index.tsx:691-713`, `RunProgress.tsx:204-308`) | +8 + **≈197**. Derived: 9 + stat grid 70 (5 Stats in `grid-cols-3` → 2 rows; `sm:grid-cols-5` never fires, the window is 600 < 640) + 12 + findings row ≈22 + 12 + buttons 32 + 8 + save 32. **→ ≈259**. Findings row is content-dependent (badges vs. a 1–2 line sentence) |
| 12 | **Failure** | `run.status === "error" && run.error` (`index.tsx:715`) | Dismiss/Try again | `RunFailure` (`RunProgress.tsx:371-403`) | +8 + **100–180** depending on `hint` and `errorId`. **→ ≈162–242** |
| 13 | **Cancelled** | `run.status === "cancelled"` (`index.tsx:726`) | Dismiss | inline row (`index.tsx:726-741`) | +8 + **42 → ≈104** |
| 14 | **Step panel** | `stepsActive` = `steps.status !== "idle"` (`index.tsx:136,635`) | `steps.reset()` via Dismiss (`index.tsx:644-647`) | `StepPanel` (`StepPanel.tsx:627-903`) | +8 + **≈182 floor**, **≈600 ceiling**. Floor: 9 + headline 17 + 8 + rail 4 + 8 + 7 rows × 16 + 6×4 = 136. Every optional part adds: `headline-fix` 14, note 14, cancel 32, armed block ≈50, approve row 32 (wraps at 3+ buttons), done row 32, file path 14 |
| 15 | **Step panel, receipts open** | `caseOutcome \|\| order \|\| sourcing` on the newest response (`StepPanel.tsx:878`) | next step response without them | `step-scroll`, `max-h-[14rem]` (`StepPanel.tsx:886`) | **+8 +224 max** — the second pre-existing clamp, and the one that keeps the window off its ceiling |
| 16 | **Deliver panel** | `stepsActive && steps.session && deliverable(history)` (`index.tsx:653`) | new session (it is `key`ed on `steps.session`) | `DeliverPanel` (`DeliverPanel.tsx:170-344`) | +8 + **≈230–420**, unclamped. 9 + title 17 + 4 rows × (32 + notes 15–30) + optional sign-in row ≈70 + optional `Agenda` block (4+ lines × 15). **Stacks on top of #14**, which is how the window reaches its 600 ceiling |
| 17 | **Docked bottom** | `overlayDock()` returns `bottom`, i.e. `dockReason === "kicad-running"` only (`overlay-dock.ts:66-89`) | any other step/run state | position only, no re-render of content | height unchanged; **position** moves to `workArea.bottom − height − 16` (`overlay-dock.ts:140-155`) |
| 18 | **Drag-pinned** | an unexplained `moved` event (`useOverlayDock.ts:150-167`) | `releasesPin` at the next run start (`overlay-dock.ts:101-103`) | — | dock decisions suppressed entirely |
| 19 | **Hidden** | `isHidden` (Windows-only shortcut path, `useApp.ts:14,33-38`) | same shortcut | root gets `hidden pointer-events-none` (`index.tsx:495-497`) | `display:none` on an ancestor ⇒ observed node measures 0 ⇒ `overlayHeightFor(0)` = 58 and `overlayWidthFor(0, compact)` = **600** (`useOverlayHeight.ts:30,47`). So hiding the pill *widens* the window to 600 before hiding it, and unhiding narrows it back |
| 20 | **Popover open** (run options, voice menu) | `PromptBar.tsx:297-326`, `VoiceControl.tsx:454-557` | trigger/Escape | Radix `Portal` → `document.body` | **Not measured at all** — see §3.9. The window does not grow, and body is `overflow:hidden` (`global.css:179-184`), so the popover is clipped at the window's bottom edge |

**Not a state:** `ReviewOutcome` (`components/ReviewOutcome.tsx`) is never
rendered — `StepPanel.tsx:35` imports only its `TONE_CLASS` table. Likewise
`VoiceButton.tsx` is referenced only by a test mock. Neither contributes height.

---

## 2. The resize path, precisely

### 2.1 The trace

1. `useOverlayHeight(!open)` returns a ref
   (`app/src/hooks/useOverlayHeight.ts:58-102`); `index.tsx:500` / `:523`
   attaches it to the wrapper `div` of whichever branch is rendering.
2. The effect (`useOverlayHeight.ts:61`, deps `[compact]`) reads `ref.current`
   once, then `observer.observe(el)` and an immediate synchronous `apply()`
   (`:88-93`).
3. Every `ResizeObserver` callback coalesces into **one** `requestAnimationFrame`
   via the `frame` guard (`:88-91`).
4. `apply()` measures
   `max(el.scrollHeight, el.getBoundingClientRect().height)` (`:73-75`) and the
   width equivalent (`:76-79`), clamps them (`overlayHeightFor` /
   `overlayWidthFor`, `:29-49`), builds `key = "WxH"`, and **returns without
   invoking if the key equals `last`** (`:80-82`).
5. Otherwise `invoke("set_window_height", { height, width })` (`:83`), which
   reaches `app/src-tauri/src/window.rs:79-96` and calls
   `window.set_size(Size::Logical(...))`. Width falls back to the Rust-side
   `OVERLAY_WIDTH = 600.0` (`window.rs:74,88`) when omitted; the hook always
   sends one.
6. The native resize emits a Tauri `resized` event, which
   `useOverlayDock`'s `onResized` (`useOverlayDock.ts:169-179`) turns into a
   **dock re-apply** — possibly a `setPosition` — 0–400 ms later.

### 2.2 How many native resize calls for one expand?

**One, in the common case.** Clicking the collapse/expand arrow flips
`expanded` → `open` → `compact`, which (a) remounts the subtree and (b) re-runs
the effect. Cleanup disconnects the old observer and cancels its pending frame
(`:95-98`); the new effect calls `apply()` synchronously with `last = ""`, so
the first measurement always invokes. The `ResizeObserver`'s own initial
observation lands on the next frame and dedupes against `last`, producing no
second call.

It becomes **N calls** whenever content arrives asynchronously, one per frame in
which the clamped size changes: a stage frame that adds a `RunProgress` row, the
`DeliverPanel` mounting under the `StepPanel`, a `commandNote` appearing, the
`ActivityFeed` opening. During a live run each new stage row is potentially its
own 16 px native resize. Nothing batches or debounces beyond the single rAF.

### 2.3 Is there any CSS transition on the card's height?

**No — refuted nowhere, confirmed by grep.** `grep -rn "transition|animate-|duration-|ease-" app/src/pages/kaleo/` returns only:

* `animate-spin` / `animate-pulse` on spinners and rail segments
  (`RunProgress.tsx:53,89`, `StepPanel.tsx:598,600,735`, `PromptBar.tsx:58`,
  `VoiceControl.tsx:315,388`, `DeliverPanel.tsx:194,226,261,296,333`);
* the `VoiceOrb`'s own injected `<style>` — `transition: r/opacity` on the SVG
  core and amplitude ring, and five `@keyframes` (`VoiceOrb.tsx:170-203`).

`app/src/global.css` has exactly one `transition` in 207 lines, on the scrollbar
thumb's background colour (`global.css:160`). The only other one in the chain is
Tailwind's `transition-all` baked into every `Button`
(`app/src/components/ui/button.tsx:7`) and `transition-all duration-300` on
`PopoverTrigger` (`components/ui/popover.tsx:22`). **Nothing animates the Card,
the wrapper, or any block's height, opacity or transform.**

### 2.4 The frame where the window and the content disagree

Expanding: React commits the tall subtree, the browser lays it out and paints
it — inside a webview that is still 58 px tall, under a root that is
`w-screen h-screen flex overflow-hidden` (`index.tsx:495`). So for at least one
frame the new content exists at full height and everything below y=58 is
**clipped, not hidden** — it simply is not there. Then `apply()` fires and the
window grows in one step, revealing the already-final layout. That is the "pop":
there is no intermediate size, ever.

Collapsing is worse, because the disagreement is on the *width* axis and the
window's origin is its top-left. The pill renders centred in a still-600 px
viewport (`justify-center`, `index.tsx:495`), then `set_size` shrinks the window
from the right edge — so the pill visibly **jumps left** by
`(600 − pillWidth)/2 ≈ 234 px`, and only then does the dock's `onResized` →
`schedule` → `apply` path re-centre it, up to `DOCK_MOVE_INTERVAL_MS` = 400 ms
later (`useOverlayDock.ts:35,129-137,143`). Expanding runs the same sequence in
reverse: grow rightwards off-centre, then snap back. This two-step is the single
most expensive artifact in the current pill↔bar swap.

### 2.5 Does `compact` flipping re-run the effect and reset `last`?

Yes, and deliberately so. `compact` is the effect's only dependency
(`useOverlayHeight.ts:99`), so a flip runs the cleanup (`:95-98`) and a fresh
setup in which `last` is re-initialised to `""` (`:65`). Consequences:

* **Correct today.** The two shapes are different DOM nodes, so re-observing is
  required, and the reset guarantees at least one invoke even if the clamped
  size happens to be identical — which it *is* for a pill↔bar swap on the height
  axis (both clamp to 58), so the reset is the only reason the *width* change
  gets sent at all in the frame it is needed.
* **Fragile for the rebuild.** The effect keys on `compact`, not on the observed
  element's identity. Any refactor that keeps `compact` stable while swapping the
  node (for instance, rendering both shapes and cross-fading them, which is the
  obvious way to build the motion you want) leaves the observer attached to a
  detached node and the window frozen at its last size. If you unify the two
  branches into one element, either keep a `[compact]`-shaped dependency or move
  to a callback ref.
* It also means a spurious `compact` flip costs a guaranteed native resize, even
  a no-op one.

---

## 3. Where motion is missing or wrong

An implementer's list, in rough order of how much it costs the "premium" feel.

1. **No height transition at all on the card.** `index.tsx:523-524` (`div.w-full`
   → `Card.w-full flex flex-col gap-2 p-2`) has no `transition`, no
   `will-change`, no wrapper with a measured height to animate. Every state in
   §1.3 arrives at its final size in one frame. §2.3 has the grep evidence.

2. **The window resize is a step function, and the content is already final
   before it happens.** Even if you add a CSS transition, it will fight
   `set_window_height`: the DOM would animate 58→260 while the native window
   jumps 58→260 immediately, so the reveal would show a growing card inside an
   already-tall transparent window. The two must be driven from one clock —
   which is why §4 proposes resizing *from state* rather than from measurement.

3. **The pill↔bar swap is a remount, so nothing can cross-fade.**
   `index.tsx:499` is a ternary between two subtrees. There is no shared element,
   no `key` continuity, and no exit phase — the pill's DOM is gone in the same
   commit the bar's appears.

4. **The pill↔bar swap moves the window twice** (§2.4): `set_size` from the
   top-left, then a dock re-centre up to 400 ms later
   (`useOverlayDock.ts:169-179` → `:129-137` → `:85-127`). Visible as a slide
   left followed by a slide back.

5. **Every content growth also re-triggers a dock move.** `onResized`
   (`useOverlayDock.ts:169-179`) calls `scheduleRef.current()` unconditionally
   (pin aside). At the top dock, `dockPosition` clamps y against
   `workArea.y + workArea.height − window.height − margin`
   (`overlay-dock.ts:143-155`), so a tall enough overlay is pushed *up* as it
   grows. So a run that reaches ≈500 px both grows and drifts.

6. **Content pops in with no enter transition, block by block.** Each optional
   block is a bare `? … : null` with no animation:
   `desk-caption` `index.tsx:579`, engine-down `index.tsx:589`, ada-caption
   `index.tsx:616`, `StepPanel` `index.tsx:635`, `DeliverPanel` `index.tsx:653`,
   busy block `index.tsx:663`, `ActivityFeed` `index.tsx:685`, result
   `index.tsx:691`, failure `index.tsx:715`, cancelled `index.tsx:726`. During a
   live run these land one at a time, each one its own instant jump.

7. **The listening swap is the one thing already right — copy it.**
   `ListeningPanel` is explicitly sized `h-9` to match the `Input` it replaces
   (`PromptBar.tsx:110`, with the reasoning at `:92-98` and
   `overlay-mode.ts:44-57`). Height-neutral, no resize, no window move. The pill
   does *not* honour the same rule: `CompactBar.tsx:79-99` swaps a 36 px button
   for a `max-w-44` span, which is a width change and therefore a native resize
   (state #2).

8. **Scroll containers appear and disappear rather than growing.** The two
   clamps — `max-h-40` on `ActivityFeed` (`index.tsx:686`) and
   `max-h-[14rem] overflow-y-auto` on `step-scroll` (`StepPanel.tsx:886`) —
   switch on with their content. The frame the receipts arrive, the box is
   already at 224 px with a scrollbar, inside a window that is still short: you
   get a scrollbar over clipped content for one frame. `global.css:142-169`
   styles a permanently-visible 8 px webkit scrollbar (no `overlay` behaviour),
   so this reads as a flash of chrome.

9. **Popovers are outside the measured tree entirely.**
   `components/ui/popover.tsx:35-48` wraps content in `PopoverPrimitive.Portal`
   with no `container`, so both the run-options popover
   (`PromptBar.tsx:297-326`, `w-96` with a `max-h-[22rem]` scroll area) and the
   voice menu (`VoiceControl.tsx:454-557`, `w-80`) render into `document.body`
   — not a descendant of the observed node. The `ResizeObserver` never sees
   them, `set_window_height` is never called for them, and `body` is
   `position:fixed; overflow:hidden` (`global.css:179-184`) inside a 58 px
   window. **Verify on screen** (this audit is static), but by construction
   these two surfaces are clipped to whatever height the bar happens to be. Any
   motion rebuild has to decide explicitly whether popovers grow the window,
   render inside the card, or get their own window.

10. **`.strip-control` and `.strip-mic` are undefined classes.**
    `VoiceControl.tsx:312` and `:364` apply them; `grep '\.strip-' app/src/global.css`
    finds only `.strip-mono` (`global.css:205`). The comment at
    `VoiceControl.tsx:361-363` describes a circle with its own surface that no
    longer exists, so the mic renders as the plain `size-9` ghost button and its
    intended shape/size is silently absent from every height calculation.

11. **`deskCaption` is never cleared** (`index.tsx:189,351`). Only `commandNote`
    has a dismiss (`index.tsx:627`). So one desk caption permanently adds
    ~14–28 px to every subsequent full-bar state for the life of the session —
    which will make any "fixed target height per state" scheme drift unless it
    is fixed or accounted for.

12. **`TOP_OFFSET_PX` and the Rust `TOP_OFFSET` disagree by 4 px.**
    `app/src/lib/overlay-dock.ts:107-108` declares `TOP_OFFSET_PX = 54` and its
    comment says it is "`TOP_OFFSET` in window.rs"; `window.rs:6` says `58`. Git
    shows `window.rs` was `54` at `e5ef6c5` and was raised to `58` in the current
    **uncommitted** working tree — the same sweep that raised
    `OVERLAY_COLLAPSED_HEIGHT` 54→58 (`useOverlayHeight.ts:20`), which is an
    unrelated number that merely happened to share the value. Effect: the window
    launches at y=58 (physical) and the first dock re-apply that actually runs
    moves it to y=54 — an unexplained 4 px hop, usually triggered by a resize
    (§3.5). **Left unfixed on purpose:** picking 54 or 58 is a positioning
    decision, and the two candidate fixes (change the TS constant, or revert the
    Rust one) are in files this audit was told not to touch for motion reasons.
    Decide it before you rebuild, or the new motion will inherit the hop.
    Note `overlay-dock.test.ts:164,179` and `useOverlayDock.test.tsx:63,101`
    reference the symbol, not the literal, so either fix passes the suite.

13. **The first dock decision is a no-op by design, so `top` is never asserted at
    launch.** `dockRef.current` starts `"top"` and the effect returns early when
    `dock === dockRef.current` (`useOverlayDock.ts:70,204`). Combined with #12,
    the bar's launch position and its "top" position are not the same place, and
    nothing notices until something else triggers `apply`.

14. **Hiding the overlay widens the window to 600.** State #19: the `hidden`
    class zeroes the measurement and `overlayWidthFor(0, true)` returns
    `OVERLAY_WIDTH` (`useOverlayHeight.ts:47`). Harmless while invisible, but it
    means unhide → 600 px window → measure → shrink to ~132, i.e. a third
    uncontrolled resize path.

15. **No reduced-motion story for the bar.** The only
    `prefers-reduced-motion` reader in the app is the orb
    (`VoiceOrb.tsx:159-168,247`, plumbed into `useMicLevel`). `global.css` has no
    `@media (prefers-reduced-motion: reduce)` block at all. Whatever you add in
    §4 needs its own guard; there is no existing one to inherit.

---

## 4. Proposed target heights — resize from state, not from measurement

The argument: of the twenty states, the ones whose height is genuinely
unknowable are few, and they are exactly the ones that already have a scroll
clamp or could get one. Everything else can be a constant, which means the
window resize and a CSS height transition can be driven from the *same* number
in the *same* frame — which is the only way to get a continuous expand.

Suggested shape: a `targetHeightFor(state)` module beside `overlay-mode.ts`
(pure, testable, the convention this codebase already uses for `overlayReason`
and `dockReason`), with the measured path kept as a fallback for the three
content-driven states.

| State | Proposal | Target | Why |
|---|---|---|---|
| 1 Idle pill | **fixed** | 58 h × 132 w | Both axes are three controls of known size. Pin the width too, so the swap is not a measurement race |
| 2 Pill listening | **fixed** | 58 h × 280 w | `max-w-44` already caps it; make the span a fixed `w-44` and the state becomes exact |
| 3 Full bar idle | **fixed** | 58 | The 54→58 clamp is the design |
| 4 Full bar listening | **fixed** | 58 | Already height-neutral by construction (`PromptBar.tsx:110`) |
| 5 Desk caption | **clamped** | 58 + 8 + `clamp(14, lines×14, 42)` | 3-line cap; add `line-clamp-3`. Also give it a dismiss (§3.11) |
| 6 Engine down | **fixed** | 110 | Fixed-height banner; give the text `truncate` so a long `baseUrl` cannot wrap |
| 7 Ada caption | **clamped** | 58 + 8 + `clamp(32, lines×17, 68)` | `line-clamp-4`; the visibility-guard sentence is the longest copy in the file |
| 8 Run in progress | **fixed** | 260 | 7 stage rows is a constant (`stages.ts`, 7 descriptors) and every row is one line |
| 9 …no events yet | **fixed** | 293 | Same, plus a fixed 2-line note; give the `<p>` a fixed height so a 1↔2 line reflow is not a resize |
| 10 …feed open | **fixed** | 420 | `max-h-40` already caps it; make it `h-40` so opening the feed is one known step, not a grow-with-content |
| 11 Result | **clamped** | 260, ceiling 300 | Stat grid is fixed at 2 rows; only the findings row varies (badges vs. sentence). Cap it and let it truncate |
| 12 Failure | **clamped** | 170, ceiling 240 | Four optional one-to-two-line fields; cap the body at `line-clamp-3` |
| 13 Cancelled | **fixed** | 104 | Fixed copy |
| 14 Step panel | **measured** | floor 240, ceiling 600 | Genuinely content-driven: 7 rows × (1 or 2 lines) × optional where-chips, plus armed/approve/error blocks. Keep measuring — but give the rows a fixed two-line height so the *common* transitions are step-shaped |
| 15 …receipts | **fixed delta** | +232 | `max-h-[14rem]` already caps it; make it `h-[14rem]` and receipts become a constant-size reveal |
| 16 Deliver panel | **measured** | floor 240, ceiling 600 | Four rows whose notes and the `Agenda` block are engine text. The only truly open-ended block; the honest fix is to give **this** a `max-h` + `overflow-y-auto` like `step-scroll`, at which point it becomes fixed too |
| 17 Docked bottom | n/a | — | Position only; must not be conflated with a size change |
| 19 Hidden | **fixed** | keep the last size | Do not resize on `display:none`; short-circuit `apply()` when the element has zero box, rather than clamping to 58×600 |
| 20 Popover open | **decide** | — | See §3.9. If popovers stay portalled, the window needs an explicit "popover open" target tall enough for `w-96` + `max-h-[22rem]` ≈ 410 px below the trigger |

**Must stay measured:** 14 and 16 (and 5/7/11/12 only until their content is
clamped). Everything else can and should be a constant, because a constant is
what lets the native resize and the CSS transition start on the same frame.

One further recommendation the numbers make obvious: **the pill and the bar are
both 58 px tall.** The expand/collapse you are rebuilding is, on the height
axis, a no-op — it is a *width* animation plus a content swap. Treat it that
way and the hard part disappears; `set_window_height` already takes a width
(`window.rs:79-96`), and animating a native window's width in steps from a rAF
loop is tractable in a way that animating its height against reflowing content
is not.

---

## 5. Constraints the rebuild must respect

1. **`OVERLAY_COLLAPSED_HEIGHT` must equal `app.height` in `tauri.conf.json`.**
   `useOverlayHeight.ts:16-20` says so; `tauri.conf.json:18` is the other half.
   Both are 58 today. The comment records *why* it is not 54: at 54 the full bar
   sat flush with the window edge and clipped the tops of the input and the
   Generate button on retina. Do not reclaim those 4 px.
2. **`OVERLAY_WIDTH` must equal `app.width` and the Rust `OVERLAY_WIDTH`.**
   `useOverlayHeight.ts:21-22`, `tauri.conf.json:17`, `window.rs:73-74`. Three
   copies of 600.
3. **Retina rounding.** `overlayHeightFor` uses `Math.ceil`
   (`useOverlayHeight.ts:35`) and `overlayWidthFor` likewise (`:48`); the Rust
   side takes a `LogicalSize` (`window.rs:87-90`) and the dock works in
   *physical* pixels with an explicit `scaleFactor`
   (`overlay-dock.ts:119-155`, `useOverlayDock.ts:96-116`). Any interpolated
   size must stay integral in logical px, or a fractional height will oscillate
   against the observer.
4. **`scrollHeight` vs `getBoundingClientRect`.** `useOverlayHeight.ts:69-75`:
   prefer `scrollHeight`, because `getBoundingClientRect` reports the *clipped*
   size when an ancestor is `h-screen overflow-hidden` — which is exactly
   `index.tsx:495` — and measuring the clipped box once left the window stuck at
   the collapsed height forever. The `max()` of the two is the working
   compromise. If you animate the card's own height, `scrollHeight` will report
   the *final* height while the box is mid-transition — which is useful (it is
   the target) but means you must not feed it back into the transition.
5. **Click-through and transparency over KiCad.** The window is `transparent`,
   `decorations: false`, `alwaysOnTop`, `shadow: false`, `acceptFirstMouse`
   (`tauri.conf.json:19-30`). There is no click-through mask: **any pixel the
   window covers is a pixel KiCad does not get**, which is the entire reason the
   pill shrinks the window's width (`useOverlayHeight.ts:39-49`,
   `window.rs:76-78`). A motion that animates the window's size must not
   overshoot, and must not hold a 600 px-wide window open "just during the
   animation" while the pill is showing.
6. **Docking.** Two homes only, chosen by `overlayDock` (`overlay-dock.ts:85-89`)
   — bottom exclusively while a KiCad-shown step is *running*. A user drag pins
   the bar until the next run starts (`useOverlayDock.ts:150-167`,
   `overlay-dock.ts:101-103`), and `onResized` un-pins a "drag" that was really a
   resize within `DOCK_MOVE_INTERVAL_MS` (`useOverlayDock.ts:169-174`). **Any new
   animated resize will emit a stream of `resized` events**, each of which
   currently schedules a dock apply and extends the settle window
   (`:175-178`). Rebuild the resize as an animation and you must either
   suppress dock scheduling for its duration or raise `DOCK_MOVE_INTERVAL_MS`,
   or the bar will chase itself.
7. **Both docks clamp into the work area** (`overlay-dock.ts:140-155`), so a
   growing window can move even at the top dock. Position and size are not
   independent.
8. **Reduced motion.** Nothing exists for the bar (§3.15). The pattern to copy is
   `VoiceOrb.tsx:159-168` — a pure `prefersReducedMotion()` with a
   `reducedMotion?` prop as a test seam — plus `data-motion="still|animated"` on
   the element (`VoiceOrb.tsx:312`) so CSS, not JS, decides. Under reduced
   motion the resize should be the current single step.
9. **`data-material` skin roles do not exist in this app.** The
   `chrome`/`panel`/`popover`/`sticky`-get-backdrop-filter,
   `tint`/`canvas`-must-not convention documented in `CLAUDE.md` belongs to the
   web SPA in `frontend/`; `grep -rn "data-material" app/src` returns nothing.
   What `app/` has instead is two global rules that apply a backdrop filter by
   `data-slot`: `[data-slot="card"]` and `[data-slot="popover-content"]`
   (`global.css:129-140`), both `backdrop-filter: var(--backdrop-blur, none)`
   with a `!important` background. Consequences for motion: the overlay Card is
   a backdrop-filter surface over live KiCad pixels, so **animating its size,
   opacity or transform re-rasterises the blur every frame** — the single most
   expensive thing you can animate here. Prefer animating the native window and
   a non-filtered inner wrapper; if you introduce a `data-material` vocabulary,
   introduce it deliberately rather than assuming the SPA's roles are present.
   `--backdrop-blur` and `--opacity` are read but defined nowhere in
   `app/src/global.css`, so both currently resolve to their fallbacks
   (`none` / `1`).
10. **The `data-testid` convention holds here too** — ids on intrinsic elements,
    repeated rows share an id and disambiguate with `data-ref`/`data-step`/
    `data-status`. The overlay's e2e and unit tests key on
    `overlay-collapse` (`index.tsx:534`), `overlay-expand`
    (`CompactBar.tsx:95`), `compact-bar` (`CompactBar.tsx:65`),
    `prompt-listening` (`PromptBar.tsx:111`) and `step-panel`
    (`StepPanel.tsx:688`). A wrapper introduced for animation must not take
    these over or displace them.
11. **`useOverlayHeight`'s effect depends only on `compact`** (`:99`). See
    §2.5 — if the rebuild stops remounting on the swap, the observer must be
    re-attached some other way.
12. **A failed resize must not be remembered as applied** — now enforced; see §6.

---

## 6. The one code change

**File:** `app/src/hooks/useOverlayHeight.ts`, in `apply()`.

**Defect:** the dedupe key `last` was written *before* the `invoke` and left in
place when it rejected. `last` is meant to record what the window *is*; on a
failed invoke the window is still its old size, but every later observation of
the same content compares equal to `last` and returns early — so a single
dropped `set_window_height` leaves the overlay clipped, silently, for the rest
of the session. That is precisely the "the run felt like nothing happened" bug
the hook's header describes (`useOverlayHeight.ts:9-11`), reintroduced through
the error path. The `.catch` is right to warn rather than throw; it was wrong to
keep the key.

Before:

```ts
      const key = `${width}x${height}`;
      if (key === last) return;
      last = key;
      invoke("set_window_height", { height, width }).catch((error) => {
        console.warn("[kaleo overlay] set_window_height failed:", error);
      });
```

After:

```ts
      const key = `${width}x${height}`;
      if (key === last) return;
      last = key;
      invoke("set_window_height", { height, width }).catch((error) => {
        // The resize did not happen, so the window is still the size it was.
        // Forget the key: `last` is a record of what the *window* is, not of
        // what we asked for, and remembering a failed ask makes every later
        // observation of the same content dedupe against a size the window
        // never took — one dropped invoke would leave the overlay clipped for
        // the rest of the session, silently, which is the exact bug this hook
        // exists to fix. Only clear it if nothing newer has been applied.
        if (last === key) last = "";
        console.warn("[kaleo overlay] set_window_height failed:", error);
      });
```

The `if (last === key)` guard matters: `invoke` is async, so a newer `apply()`
may already have recorded a different size, and clearing unconditionally would
re-send it.

Verified with `cd app && npx vitest run src/hooks src/lib` (41 files, 633 tests,
all passing) and `npx tsc --noEmit` (clean).

**Not fixed, deliberately:** the `TOP_OFFSET_PX` 54 vs `TOP_OFFSET` 58 mismatch
(§3.12) — a real defect, but the fix is a positioning decision in files this
audit was scoped out of. Nothing else in this document is a code change.

---

## 7. The motion vocabulary (appended 2026-09-06)

Everything §3 lists as missing is answered by a closed set of named classes in
**`app/src/motion.css`**, imported from `app/src/global.css`. The set is
deliberately small and applied *by name*: no component should ever carry its
own duration, and every number below lives in exactly one place.

**Two invariants the vocabulary is built to keep.**

* Only `opacity` and `transform` are animated. §5.9 is the reason — the overlay
  `Card` is a `backdrop-filter` surface over live KiCad pixels, and those are
  the two properties the compositor can run without re-rasterising the blur.
  The single exception is `height`, and only under `.kv-list` below, which
  never leaves the DOM.
* Nothing here can resize the native window. A transform does not change an
  ancestor's border box, `scrollWidth` or `scrollHeight` — which is exactly
  what `useOverlayHeight`'s `apply()` measures (`useOverlayHeight.ts:69-79`) —
  so applying any class below is invisible to the `ResizeObserver`, and the
  §3.2 "DOM animates while the window jumps" fight cannot occur.

**Tokens** (`:root` in `motion.css`): `--kv-dur-swap` 90ms, `--kv-dur-enter`
150ms, `--kv-dur-exit` 110ms, `--kv-dur-size` 100ms, `--kv-stagger` 28ms,
`--kv-ease-opacity` easeOutCirc, `--kv-ease-move` easeOutExpo. The register is
Flow Launcher's (160/360/560 with a circular ease on opacity), taken at or
below its short tier throughout: 360 and 560 are Flow's *window* fades, and
everything here is a 4 px move inside an already-visible card. Full
justification for each number is in the file's header comment.

### The classes

| Class | What it does | Duration | Apply to |
|---|---|---|---|
| `.kv-swap-in` | Opacity 0→1, no offset. A crossfade of two elements sharing one slot; an offset is *wrong* here because they would cross through each other. | 90 ms, easeOutCirc | The incoming half of a substitution — the pill's chevron ↔ listening status (`CompactBar.tsx`), `ListeningPanel` ↔ `Input` in `PromptBar` |
| `.kv-swap-out` | Opacity 1→0. Only runs where the implementer keeps the outgoing node mounted (a presence wrapper or a delayed unmount); a plain `? :` ternary has no exit phase and this class will simply never fire. | 110 ms, easeOutCirc | The outgoing half, where one exists |
| `.kv-shape-in` | `translateX(-4px)`→0 plus the opacity, i.e. the content arrives along the same axis the native window grows on (top-left origin, §2.4). | 150 ms, easeOutExpo + easeOutCirc | The root of whichever top-level shape is mounting — pill or bar. **Not** on the node `useOverlayHeight` observes; on its child |
| `.kv-settle` | `translateY(4px)`→0 plus the opacity. The card is a vertical stack, so a block that lands in it arrives from below. | 150 ms | A single block appearing mid-run: result card, failure block, engine-down banner, `StepPanel`, `DeliverPanel` (§3.6's list) |
| `.kv-settle-group` | Same, applied to the container's direct children with a per-child stagger, capped at five steps (140 ms total lead-in) so a seven-row feed does not become a countdown. | 150 ms + 0/28/56/84/112 ms | `ActivityFeed`'s row container, `RunProgress`'s stage rows |
| `.kv-list` | The cmdk height mechanism: reads `--kv-list-height` (written from a `ResizeObserver` on an inner sizer) and transitions `height` to it. Unset ⇒ `height: auto`, i.e. today's behaviour, so it is safe to apply before wiring the observer. | 100 ms ease | The activity feed's clamp box, the `step-scroll` receipts box |

`.kv-list` is adapted from **cmdk** (`pacocoursey/cmdk`, **MIT** — compatible
with this repo's GPL-3.0), which writes `--cmdk-list-height` from a
`ResizeObserver` and consumes it with `transition: height 100ms ease`. Both the
mechanism and the 100 ms are kept verbatim; the property is renamed to the
`--kv-` prefix the rest of this file uses. The whole animation stays inside the
DOM and never becomes a native resize.

### Reduced motion

`@media (prefers-reduced-motion: reduce)` **disables** every class above
(`animation: none`, `transition: none`), rather than shortening it. This is a
floating always-on-top window over someone's work and is the case the query
exists for; elements still appear, they just do it in one frame, which is the
overlay's behaviour today. This answers §3.15 and §5.8.

A manual seam exists alongside it: `data-motion="still"` on any ancestor (or on
the animated element itself) has the same effect, matching the attribute
`VoiceOrb` already uses (`VoiceOrb.tsx:312`) so a component with its own reason,
or a test, can opt out without a media query.

### What is applied so far

`CompactBar.tsx` only: `.kv-shape-in` on the pill's root, `.kv-swap-in` on both
halves of the chevron ↔ listening-status substitution. Two tests in
`CompactBar.test.tsx` pin which class sits where and assert the pill carries no
`transition-*`/`blur-`/`shadow-` utility of its own. `index.tsx` and
`PromptBar.tsx` are another lane's to apply; the classes above need nothing
from this file's author to be used there.

Nothing was removed: the `VoiceOrb`'s injected SVG keyframes, the `animate-spin`
and `animate-pulse` spinners, and the scrollbar-thumb transition all stand.

**Not verified on screen.** The vocabulary was written and unit-tested from a
shell with no macOS Accessibility permission, so nothing here was driven live.
The four things a human should watch, in order of how much they would cost:
(1) the pill ↔ bar swap — does the 4 px lead-in read as the content following
the window, or as a second, later move on top of the §2.4 slide-and-snap;
(2) the chevron ↔ listening crossfade against the *width* resize it rides on —
90 ms was chosen to finish first, and if the native resize is slower the text
will appear to settle before the frame does; (3) the settle stagger on a live
run's activity feed — 28 ms × 5 may be invisible at seven rows or may read as a
ripple; (4) shimmer: watch a settling block's text edges against a blurred
KiCad backdrop on retina, which is what the no-`scale()` rule is there to
prevent.
