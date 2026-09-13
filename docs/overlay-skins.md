# Overlay skins — build notes

Working notes for the four overlay skins and the two settings features, produced by a research pass that read the real open-source implementations rather than inferring them. Each section names the projects actually opened, what the real code contradicted, and the concrete change to make in this repo.

These are engineering notes, not a decision. Nothing here has been committed. Claims marked **Unverified** were flagged by the researcher as inference — treat them as leads, not facts.

| Feature | Ships as | Blocked on |
| --- | --- | --- |
| Plain | Subtraction from `PromptBar.tsx` | nothing |
| Spotlight | New shell, same state | `window-vibrancy` as a direct dep |
| Orb | Scale up `VoiceOrb.tsx` | WebGL-in-transparent-window unverified |
| Terminal | Buffer + scoped shell seam | `shell:allow-spawn` capability, `@tauri-apps/plugin-shell` not installed |
| Follow KiCad | `NSWorkspace` observer + `tauri-plugin-store` | nothing |
| Notifications | `tauri-plugin-notification` | nothing (but no click-back is possible) |

---

## Plain

**Read:** iamsrikanthnani/pluely, vendored in kaleo/app (GPL-3.0) · ahkohd/tauri-macos-spotlight-example (MIT) · ahkohd/tauri-nspanel v2/v2.1 (Apache-2.0) · sohzm/cheating-daddy (GPL-3.0) · tauri-apps/tauri config schema + @tauri-apps/api (Apache-2.0 OR MIT)

This is the resting frame of Kaleo's actual overlay window, not a stylised bar: 600 × 58 with decorations:false, transparent:true, shadow:false, focus:false, acceptFirstMouse:true and app.macOSPrivateApi:true, read out of app/src-tauri/tauri.conf.json. The lineage is Pluely (GPL-3.0, vendored into app/) which is the same architecture as ahkohd's MIT tauri-macos-spotlight-example: every bit of "glass" here is CSS on a transparent window — neither project uses Tauri windowEffects / NSVisualEffectView at all, and index.html sets body background-color: transparent !important to let it through. The dashed rectangle is drawn at the window's true bounds because that rect is a real mouse target over KiCad; the drag rail on the left is the single data-tauri-drag-region grip from DragButton.tsx. The researcher could not read a pristine upstream Pluely tree (v1 is closed source; the fork was squashed into commit e5ef6c5), so "upstream" here means the vendored copy on disk.

### What the real source contradicted

- Collapsed height is 58, not the round 54 a mock would guess — Pluely's useWindow.ts toggled 54 <-> 600, and Kaleo raised it to 58 (OVERLAY_COLLAPSED_HEIGHT) because 54 clipped the tops of the h-9 controls on retina. The mock bar is 58 px exactly.
- The bar is not a floating rounded pill on an infinite canvas: it is a window, and the window rect is exactly the bar. So the mock draws the 600 × 58 bound as a dashed outline — that rect eats clicks meant for KiCad on every transparent pixel, which is why overlayWidthFor shrinks the compact pill to its content (min 120 px). A hand-rolled mock implies free space around the bar that does not exist.
- The glass is CSS on a transparent window, not a native material. Neither Pluely nor the spotlight example touches windowEffects; hand-rolling would reach for underWindowBackground/hudWindow, which stacks two translucency systems on a transparent:true window (and several Effect values are deprecated since macOS 10.14, while blur/acrylic are documented as slow while dragging — which this bar does constantly).
- alwaysOnTop is not what keeps the bar from stealing focus. The spotlight example does not even set it; the float comes from PanelLevel::Floating (4) plus StyleMask nonactivating_panel (1<<7). A mock captioned 'always on top' misstates the mechanism — the caption here names the panel level and mask instead.
- The drag handle is one ghost grip button, not a full-width titlebar strip. Tauri's data-tauri-drag-region applies only to the exact element it is on, so Kaleo's DragButton repeats it on the inner icon — the opposite of Electron's -webkit-app-region:drag (cheating-daddy), which does cascade to the whole header.
- The engine indicator has three states, not two: EngineDot renders a grey dot while engine.lastCheckedAt === null. The resting mock is that unknown state, and per house rules it is carried by shape (dashed hollow ring) plus the word UNKNOWN, never by colour alone.
- House rules over source habits: Pluely's bar keeps a logo and a licence/purchase affordance in the strip and colours its primary action. This frame drops all of it — no logo, no hue, and because nothing is typed and the engine is unprobed, the paid control is absent rather than greyed, so the resting frame contains zero filled elements and states no price.

### Build note

Everything is already in app/.

(1) app/src-tauri/tauri.conf.json — keep width 600 / height 58 / transparent true / decorations false / shadow false / focus false / acceptFirstMouse true / visibleOnAllWorkspaces true / skipTaskbar true, and keep app.macOSPrivateApi:true (without it macOS silently renders the window opaque). Do not add windowEffects; it would stack NSVisualEffectView under the CSS backdrop.

(2) app/src-tauri/src/lib.rs:246-258 — replace the hand-written cocoa constants (set_level(4), set_style_mask(1<<7), set_collection_behaviour(FullScreenAuxiliary|CanJoinAllSpaces)) with tauri-nspanel v2.1's typed API: tauri_panel!{ panel!(KaleoPanel { config: { is_floating_panel: true } }) }, PanelLevel::Floating.value(), StyleMask::empty().nonactivating_panel().into(), CollectionBehavior::new().full_screen_auxiliary().move_to_active_space().value(); and move the Cargo.toml dep off the moving `branch = "v2"` to v2.1 (or pin a rev).

(3) Add app.set_activation_policy(tauri::ActivationPolicy::Prohibited) in setup() in lib.rs — today the policy is only set later from shortcuts.rs set_app_icon_visibility, so the first launch still steals focus.

(4) app/src/components/DragButton.tsx — data-tauri-drag-region must stay on BOTH the Button and the inner GripVerticalIcon; the attribute does not inherit to descendants.

(5) app/src/hooks/useOverlayHeight.ts owns all resizing via the Rust command set_window_height(window, height, width: Option<u32>) in src-tauri/src/window.rs; keep Math.max(scrollHeight, rect.height) and keep overlayWidthFor's compact shrink (min 120) so the transparent strip is not a 600 px click sponge. Position stays Rust-side: window.rs TOP_OFFSET = 58, center_x from primary_monitor().size(). No new plugin or permission string is needed for any of this — global-shortcut is the only plugin in play and it is already registered.

### Unverified

- No one read a pristine upstream Pluely tree — v1 is closed source and the fork was squashed into commit e5ef6c5 — so everything labelled "upstream Pluely" is the already-renamed copy vendored into app/, and which lines were changed on the way in is unknown.
- It was never confirmed that Kaleo's bar renders any translucency today: the "glass is CSS on a transparent window" claim comes from the window config and component logic, not from the Tailwind classes that would actually produce a blur.
- The tauri-nspanel v2.1 migration in the build note was never built or run — how tauri_panel! behaves on a window Tauri created from tauri.conf.json is inferred from the spotlight example, which is a different setup.
- Only PanelLevel's values were read directly; the StyleMask and CollectionBehavior bitflags quoted here were taken on trust from objc2-app-kit rather than opened.
- Whether Tauri issue #11605 is already fixed in the version Kaleo pins was not checked against the changelog or Cargo.lock.

---

## Spotlight

**Read:** ahkohd/tauri-macos-spotlight-example (MIT) · ospfranco/sol (MIT) · tauri-apps/window-vibrancy (MIT/Apache-2.0) · dip/cmdk (MIT)

The panel here is the ahkohd/tauri-macos-spotlight-example (MIT) window layer with sol's (MIT) visual layer on top: a WebviewWindow converted to an NSPanel via tauri-nspanel, style mask NSWindowStyleMaskNonActivatingPanel (1<<7), floating level, collection behaviour fullScreenAuxiliary | canJoinAllSpaces — which is very close to what Kaleo's lib.rs already does. The blur is the piece Kaleo is missing and the piece a CSS mock cannot honestly show: real behind-window blur is an NSVisualEffectView with material .headerView, blendingMode .behindWindow, state .active, which window-vibrancy inserts *below* the webview via addSubview_positioned_relativeTo(…, NSWindowOrderingMode::Below, None). The dashed rounded rect behind each bar stands in for that native view; its CSS backdrop-filter is a depiction, not the technique. The field is cmdk's core Input — one bare `<input>` with cmdk's attribute set and border:none / outline:none, sized to the Raycast preset (15px, 8px 16px) inside sol's 42px row — with Kaleo's `h-9 rounded-md border border-input/50 bg-muted/30` wrapper deleted. Note the researcher could not verify Raycast's real panel geometry or any numeric NSVisualEffectView blur radius; none is claimed here.

### What the real source contradicted

- A hand-rolled mock reaches for `backdrop-filter: blur()` on the bar. That is page blur, not window blur — it blurs whatever the webview already painted, never the KiCad canvas behind the window. window-vibrancy's src/macos/vibrancy.rs proves the real move: build an NSVisualEffectView, setBlendingMode(BehindWindow), and add it with addSubview_positioned_relativeTo(&blurred_view, NSWindowOrderingMode::Below, None) — BELOW the webview, so a transparent WKWebView sits on it. Neither sol nor window-vibrancy uses CSS blur anywhere.
- The corner radius was CSS-only in the hand-rolled version, which leaves a square blurred rectangle poking out behind rounded HTML. window-vibrancy takes radius as an argument and calls setCornerRadius(radius.unwrap_or(0.0)); sol's BlurView.swift sets layer?.cornerRadius together with layer?.masksToBounds = true. The 12px here is annotated as a native setCornerRadius value, not a border-radius.
- There is no numeric blur radius. AppKit exposes only material, blendingMode and state — sol's Panel.swift sets .headerView / .behindWindow / .active and nothing else. Any '30px blur' figure (including the one in this mock's depiction layer) is a CSS approximation, so the annotation names the material enum (HeaderView = 10) and the state enum (Active = 1) instead of a px number.
- Kaleo's PromptBar.tsx line 110 wraps the field in `h-9 ... rounded-md border border-input/50 bg-muted/30 px-3`. cmdk's core index.tsx renders a single Primitive.input with a bare `cmdk-input=""` attribute and ships zero CSS; both its Raycast and Linear presets set `border: none; outline: none` on the input and put any border on the ROOT. The inner bordered box is the hand-rolled tell and it is gone here — one bare <input>, radius on the panel only.
- The focus ring was an afterthought. sol passes enableFocusRing={false} on its native TextInput; the webview equivalent is `outline: none` (plus :focus-visible), which every cmdk preset sets. Without it a macOS-blue ring appears inside a borderless panel.
- cmdk's Linear preset sets `caret-color: #6e5ed2`. Copied literally that is a second hue in a bar that must sit on both KiCad grounds — HOUSE RULE WINS: the caret takes --k-hi from the pinned palette, and no colour in this section flips with the OS theme (sol's global.css does exactly the opposite, swapping .bg-window between #FFFFFFAA and #00000066 on the dark-mode class).
- sol's Panel.swift is its own NSWindowDelegate and calls PanelManager.shared.hideWindow() on windowDidResignKey — correct for a launcher, fatal for Kaleo, which must stay up while KiCad holds focus. The mock's annotation states the inverted contract: hide on the global shortcut only, and keep set_hides_on_deactivate(false), which Kaleo's lib.rs already has.
- Positioning was assumed to be 'centre it'. ahkohd's window.rs centres on the monitor under the cursor (get_monitor_with_cursor, sizes via to_logical(scale_factor)), and sol uses screen.visibleFrame — which excludes the menu bar and Dock — and deliberately does NOT centre vertically: y = midY - height + 0.3 * visibleFrame.height. Kaleo's window.rs uses primary_monitor() and monitor.size(), which mis-places on a laptop-plus-external rig and measures TOP_OFFSET from under the menu bar.
- The hand-rolled mock drew a greyed-out run button when nothing was typed. HOUSE RULE WINS over every source project's habit of a persistent submit affordance: the eeschema stage has no paid control at all, and the pcbnew stage's single filled .paid element states the price for Enter to arm while ⌘↵ commits — no source project models a spend gate, so none of this came from them.
- Engine health was a coloured dot. Three states, not two, and shape carries each one: filled+ring for up, dashed hollow for unknown-before-first-probe, slashed hollow for down — legible with colour stripped, which a hue-only dot is not.
- sol's baseSize is 700x450 and ahkohd's window is 800x250; the mock keeps Kaleo's real 600x58 strip from tauri.conf.json rather than importing a launcher's geometry, since Kaleo is a bar over KiCad, not a full-screen launcher.
- The panel's shadow was omitted (Kaleo ships `"shadow": false`). sol sets hasShadow = true alongside isOpaque = false — a vibrancy panel with no shadow reads as a sticker on the canvas, so the mock draws the drop shadow on the native layer, not on the HTML shell.

### Build note

Touch three files.

(1) `app/src-tauri/Cargo.toml`: add `window-vibrancy = "0.6"` explicitly — 0.6.0 is already resolved in Cargo.lock as a transitive dep of tauri's macos-private-api feature, but `use window_vibrancy::…` will not compile until it is a direct dependency (unverified whether the lock's version resolves cleanly as a direct dep; check the build).

(2) `app/src-tauri/src/lib.rs`, in the existing `#[cfg(target_os = "macos")]` init block around lines 215-260 where `window.to_panel()`, `set_level(4)`, `set_hides_on_deactivate(false)` and `set_style_mask(1 << 7)` already run: call `window_vibrancy::apply_vibrancy(&window, NSVisualEffectMaterial::HeaderView, Some(NSVisualEffectState::Active), Some(12.0))?` BEFORE `to_panel()`, and drop `"shadow": false` in tauri.conf.json to `true` (sol sets hasShadow = true; a vibrancy panel with no shadow reads as a decal). Leave `transparent: true` — the effect view only shows through a transparent WKWebView. Keep the existing `panel_delegate!` but do NOT wire `window_did_resign_key` to hide: sol hides there because it wants to; Kaleo must survive KiCad taking focus.

(3) `app/src-tauri/src/window.rs`: replace `primary_monitor()` + `monitor.size()` in `position_window_top_center` with the cursor monitor — `window.available_monitors()` filtered by cursor position (or `tauri_nspanel`'s `monitor::get_monitor_with_cursor()`), and use the *work area* (`monitor.work_area()`, the equivalent of AppKit `visibleFrame`) so TOP_OFFSET = 58 is measured below the menu bar rather than under it. Frontend: `app/src/pages/kaleo/components/PromptBar.tsx` line 110 — delete `h-9 rounded-md border border-input/50 bg-muted/30 px-3` from the field wrapper and let the bare input carry `border-none outline-none bg-transparent w-full text-[15px] px-4 py-2`. The toggle stays on `tauri-plugin-global-shortcut`; its capability file needs `global-shortcut:allow-register`, `global-shortcut:allow-unregister` and `global-shortcut:allow-is-registered` in `app/src-tauri/capabilities/*.json`.

### Unverified

- Raycast's real panel geometry was never found in any official doc, so the "Raycast preset" sizing here comes from a cmdk clone stylesheet, not from Raycast itself.
- There is no verified corner radius for sol's panel — Panel.swift sets none and the value BlurView takes from JS was never traced — so the radius shown is a choice, not a copied number.
- window-vibrancy 0.6.0 appears only as a transitive entry in Cargo.lock; nobody built Kaleo with it as a direct dependency, so step one of the build note may not compile as written.
- Kaleo's current radius and backdrop values were never read in full — only PromptBar.tsx and CompactBar.tsx were grepped for keywords — so "delete the bordered wrapper" rests on a partial reading.

### Numbers with no source

- the setCornerRadius 12.0 annotated as a native (not CSS) value on the Spotlight panel — sol's Panel.swift sets no cornerRadius and the researcher recorded no verified radius from any source

---

## Orb

**Read:** elevenlabs/packages convai-widget-core (MIT, © 2025 ElevenLabs) · livekit/components-js hooks + radial visualizer (Apache-2.0 — aura shader excluded, Polyform Non-Resale) · openai/openai-realtime-console@websockets wavtools (MIT, © 2024 OpenAI) · kopiro/siriwave (MIT, © 2020 Flavio De Stefano)

Every genuine orb the researcher opened is a WebGL fragment shader, not a CSS gradient. This mock reproduces the *resting frame* of ElevenLabs' convai-widget-core Orb.ts (MIT) in SVG, because the page is static and no canvas is available: the disc is its 4-stop grayscale ramp mapped to the shipped colors #2792DC / #9CE6E6, the seven soft ovals are its seven polar-centred ovals at softness 0.4 with axes a = noise*1.5, b = noise*4.5, and the two rims are its sharpRing (start 1.0, width 0.5) and smoothRing (start 0.9, width 0.3) with feTurbulence standing in for the value-noise texture it fetches. The state ladder's numbers are LiveKit's real `use-agent-audio-visualizer-aura.ts` table (speed/scale/amplitude/frequency/brightness per state, 0.5s easeOut, `animateScale(0.2 + 0.2*volume)` while speaking only), and the amplitude math quoted is `useTrackVolume` — RMS over getByteFrequencyData at fftSize 32, smoothing 0, sampled every 1000/30 ms. Nothing here animates: the real thing is time-driven at 60fps, and this is honestly the frozen frame rather than a CSS impersonation of it. LiveKit's *aura shader source* is deliberately not reproduced — that one file is Polyform Non-Resale 1.0.0 (© UNCRN LLC), not Apache-2.0, and cannot ship in Kaleo.

### What the real source contradicted

- A hand-rolled CSS orb is a radial-gradient circle with a scale keyframe. Every real orb that was opened is a WebGL2 fragment shader — ElevenLabs draws a 4-vertex TRIANGLE_STRIP fullscreen quad and puts all the art in one .frag; LiveKit's aura runs a 4-layer domain warp sampled 36 times per pixel. The disc here is drawn as 7 blurred polar ovals plus two turbulence-displaced rings because that is what the shader actually composites, not a single gradient.
- Amplitude is not one number you can invent. LiveKit uses RMS (`Math.sqrt(sum/n)/255`) over getByteFrequencyData with fftSize 32 and smoothingTimeConstant 0; ElevenLabs uses a plain arithmetic mean over voice-band bins with fftSize 2048 and smoothing 0.8. They are not interchangeable: a mean over a 2048-point FFT with no band limit reads as a dead orb because most bins are silent, and RMS over 16 bins jitters unless the AnalyserNode itself smooths. A mock keyframe hides that the FFT size and the smoothing constant have to be chosen as a pair.
- Attack and release are asymmetric in all four real projects, and a symmetric CSS ease is wrong. ElevenLabs clamps attack-only (`if (targetSpeed > speed) speed = targetSpeed`), voiceorb does `current += (target-current)*0.25`, siriwave lerps at a fixed 0.1/frame, Kaleo already does 0.82/0.18. A single `transition: transform .3s ease` makes speech look like a slow pulse instead of a voice.
- ElevenLabs' orb does NOT breathe with the microphone, which is the opposite of what a hand-rolled mock assumes. `Orb.ts` computes `this.speed = 0.2 + (1 - (output-1)^2) * 1.8` and calls `gl.uniform1f(..., 'uInputVolume', input)` — but no such uniform exists in the checked-in OrbShader.frag, so those calls no-op on a null location. The only place volume moves anything is `Avatar.tsx`, as CSS `transform: scale()` on two divs: `1 - inputVolume*0.4` and `1 + outputVolume*0.4`.
- State is a parameter table, not a set of CSS classes. LiveKit ships explicit numbers per state (idle speed 10 / scale 0.2 / amplitude 1.2 / brightness 1.0; listening 20 / 0.3 spring bounce 0.35 / 1.0 / brightness animated 1.5→2.0; thinking 30 / 0.3 / 0.5 / 0.5→2.5; speaking 70 / 0.3 / 0.75 / 1.5), with DEFAULT_TRANSITION { duration: 0.5, ease: 'easeOut' } and a 0.35s mirror-repeat pulse. Those are in the ladder here instead of hand-picked durations.
- LiveKit maps volume to the orb only while `state === 'speaking'` and only when no easing animation currently owns the value (`!scaleMotionValue.isAnimating()`). A hand-rolled mock pipes level straight into scale at all times — which in Kaleo would reintroduce the exact lie VoiceOrb.tsx's header forbids, an orb reacting as though listening when the mic is not open.
- The orb is not the thing that owns the microphone. Every OSS example opens its own AudioContext because it also owns the stream; Kaleo's wake-word.ts already holds the only one, and a second getUserMedia on macOS is a second permission prompt. The technique to copy is the analyser *math*, not the analyser *ownership*.
- OpenAI has no orb to imitate. The current openai-realtime-console (WebRTC main) contains no visualizer, canvas or AnalyserNode at all, and openai-realtime-agents has none either; the only visualization OpenAI ever shipped in the open is flat 2D bars on the archived `websockets` branch — `drawBars(canvas, ctx, values, '#0099ff', 10, 0, 8)`, 10 peak-picked bars. Any mock captioned 'like ChatGPT voice mode' is describing code that does not exist publicly.
- ElevenLabs loads its Perlin texture from `https://storage.googleapis.com/eleven-public-cdn/images/perlin-noise.png` with `crossOrigin = 'anonymous'`, falling back to a 1×1 grey texel. In a Tauri overlay that is a CSP entry and a visibly broken orb whenever KiCad is used offline — so the noise here is generated locally (feTurbulence in the mock, a baked data URI in the app).
- HOUSE RULES WIN over LiveKit's state vocabulary: LiveKit separates states mostly by speed, brightness and hue, which is unreadable at Kaleo's 18 px. The ladder keeps Kaleo's shape-level channel — dotted ring for unknown, broken ring plus a slash for engine down, rotating arc for thinking, two haloes for speaking — so state survives a monochrome screenshot. It also keeps three engine states, not LiveKit's binary connected/disconnected.
- HOUSE RULES WIN on motion: LiveKit, ElevenLabs and voiceorb all run an unconditional requestAnimationFrame loop and none of them honour prefers-reduced-motion. This mock has no animation at all — the resting frame is the deliverable — and the app keeps VoiceOrb's existing `still` / `data-motion` path.
- HOUSE RULES WIN on the paid control: both strip frames here show it absent, not greyed, because nothing is typed in one and the engine is unknown in the other. No source project has this constraint; it is why the orb frame carries no filled element beside it.

### Build note

Touch `app/src/pages/kaleo/components/VoiceOrb.tsx` and `app/src/hooks/useMicLevel.ts` only — do not add a mic tap. Keep `wake-word.ts` as the sole `getUserMedia` owner and keep publishing on the `publishMicLevel` bus; the change inside `useMicLevel.ts` is to replace `normaliseMicPeak`'s 0-128 peak curve (`(peak-3)/45` ** 0.7) with LiveKit's RMS shape, which means the publisher in `wake-word.ts` must switch its analyser from `getByteTimeDomainData` at fftSize 1024 to `getByteFrequencyData`, and publish `Math.sqrt(sum(a*a)/n)/255`. Because Kaleo's analyser is shared with wake detection you cannot use LiveKit's fftSize 32 / smoothing 0 — the wake spotter needs the resolution — so keep fftSize 1024, set `smoothingTimeConstant = 0.55` (LiveKit's own aura override) and do the RMS over the 100 Hz-8 kHz bin slice (ElevenLabs' voice band, `hzPerBin = sampleRate/2/binCount`). Keep the existing 0.82/0.18 release with instant attack; that already matches ElevenLabs' `if (target > current) current = target` attack clamp. For the renderer: if you go WebGL, port ElevenLabs' Orb.ts (MIT, 235 lines, zero deps, `getContext('webgl2', {depth:false, stencil:false})`, QUAD_POSITIONS [-1,1,-1,-1,1,1,1,-1], TRIANGLE_STRIP 4) into a new `app/src/pages/kaleo/components/OrbCanvas.tsx`, bake the Perlin texture as a data URI generated at build time instead of `https://storage.googleapis.com/eleven-public-cdn/images/perlin-noise.png`, and add the real uniforms `uInputVolume`/`uOutputVolume` to the fragment shader — they do not exist in the checked-in ElevenLabs shader, so its `gl.uniform1f` calls silently no-op and a straight port gives you a beautiful orb that never reacts. Before any of that, open `app/src-tauri/tauri.conf.json` and confirm two things nobody has verified: that `app.windows[].transparent = true` still composites a WebGL canvas on macOS WKWebView, and that `app.security.csp` permits the inline shader strings and the data-URI texture (`img-src 'self' data:`). If either fails, ship LiveKit's Apache-2.0 `agent-audio-visualizer-radial` shape instead — 12 dots at radius 6 px, `rotate(θ) translateY(6px)`, no GPU — which is exactly the 18 px footprint VoiceOrb already occupies. Gate the whole rAF loop on the existing `still` / `data-motion` path so `prefers-reduced-motion` still renders one frame and stops; none of the OSS orbs do this and all three would regress it.

### Unverified

- It was never tested whether a WebGL canvas composites at all in a transparent, always-on-top Tauri window on macOS WKWebView — the single question that decides whether the shader orb is shippable.
- Kaleo's CSP was never read, so the inline shader strings and the baked data-URI noise texture may simply be blocked.
- No frame-rate or GPU-cost figure was measured for any of these shaders; the performance warnings are structural reasoning, not benchmarks.
- Why ElevenLabs computes this.speed and never uploads it to the shader is unknown — it may be vestigial code or an unshipped variant, so "the orb does not breathe with the mic" describes the checked-in shader, not necessarily the shipped widget.
- Not every caller of ElevenLabs' updateVolume was traced, so the claim that volume moves nothing but two CSS scales holds for the files read, not for the whole widget.
- No Rive or Lottie orb and no public OpenAI voice-mode orb source was ever found or opened, so nothing here compares against those approaches even where a reader might expect it to.

### Numbers with no source

- the "hold ~2 s" duration on the heard-the-wake-word frame — no source project and no Kaleo constant was recorded for it

---

## Terminal

> ### Superseded, 2026-09-06 — the negative finding was wrong
>
> Everything below about `tauri-plugin-shell` is accurate: it really does give
> every child `Stdio::piped()` and never opens a pty. The error was the leap
> from *that plugin* to *the platform*. The plugin is not the only way to start
> a process from `src-tauri`.
>
> **`portable-pty` 0.9 (the WezTerm crate, MIT) opens a real pty directly**, and
> `marc2332/tauri-terminal`, `Shabari-K-S/terminon` and `tauri-plugin-pty` (MIT)
> all do exactly that. Electron's Hyper faces the same problem and answers it
> the same way with `node-pty`; the Rust equivalent existed the whole time.
>
> This is now built, in `app/src-tauri/src/pty.rs`, and proven rather than
> argued: `pty::tests::the_child_believes_it_is_talking_to_a_terminal` spawns a
> real process on the pty and asserts `test -t 1` succeeds — the exact check
> the piped-stdio runner fails. So the skin is a literal terminal: `vim`,
> `htop`, tab completion, Ctrl-C and the user's own prompt all work.
>
> Three corrections to the design below follow from it:
>
> - **xterm.js is back in.** The ink-style "committed scrollback plus a live
>   input line" was the right architecture *for a command runner*. It cannot
>   render a full-screen program, and once there is a pty there are full-screen
>   programs. `@xterm/xterm` 5.5 + `@xterm/addon-fit` are installed.
> - **Output goes over a `tauri::ipc::Channel`, not `app.emit`.** Tauri's docs
>   say the event system "is not designed for low latency or high throughput"
>   and may deliver out of order; for terminal bytes that is scrambled output.
> - **`resize` is a real command.** A pty left at the 24×80 default is the most
>   common bug in every terminal-in-Tauri repo named above.
>
> The Ada half shares the input line rather than sitting in a pane beside it,
> following **Butterfish** (MIT) — the only prior art that does. The first
> character the user typed decides: a capital letter asks Ada, `!` sets her to
> work, anything else runs. Two rules are ours, both covering holes in that
> set: a leading space forces the shell (`Rscript`, `Xvfb`, `Setup.exe` are
> real capitalised commands), and nothing routes at all while the alternate
> screen is up (inside `vim`, `:wq` is not a shell command and `Quit` is not a
> question). Rules and tests: `app/src/lib/terminal-sigil.ts`.
>
> **It is now selectable.** `app/src/lib/overlay-skin.ts` is the catalogue and
> the stored choice; `useOverlaySkin` carries it between the two webviews on a
> `storage` event (Settings and the overlay are separate React trees, so a
> context cannot); **Settings → Overlay** picks it. The terminal skin replaces
> the bar rather than sitting inside it, which created one trap worth naming:
> the settings button lives in the bar, so the terminal card carries its own
> "Back to the bar" control and the drag region the bar normally provides.
> Only the Terminal skin is built — Bar is the existing overlay, and Spotlight
> and Orb are still listed-but-unimplemented, so picking one of those today
> leaves you on the bar.
>
> **Ada is wired.** `app/src/lib/silkscreen/chat.ts` drives `POST /chat/stream`,
> which existed on the engine and had no client. One property of that endpoint
> shapes the whole module: `service/app.py` hands the orchestrator a `generate`
> callable, so **a typed sentence can start a paid board run**. So every frame
> is described into the terminal as it arrives rather than the shell sitting
> silent for minutes, and the outcome carries `ranBoard` so a run is reported
> as a run and never as a chat reply that took a while.
>
> **Security posture, stated because it changed.** `cli.rs` runs an allowlisted
> tool; `pty.rs` runs the user's own shell with their own privileges — the same
> thing Terminal.app gives them. That is the feature and it is also the risk,
> so a session exists only while the skin is open, nothing model-generated is
> injected into one (a proposed command is staged as text for the user to press
> Enter on), sessions are capped at 8, and every one is killed on close.

**Read:** tauri-apps/plugins-workspace — tauri-plugin-shell 2.3.1 (Apache-2.0 OR MIT) · xtermjs/xterm.js 6.0.0 (MIT) · vercel/hyper 4.0.0-canary.5 (MIT) · microsoft/node-pty 1.1.0 (MIT) · vadimdemedes/ink 7.1.1 (MIT)

Built from tauri-plugin-shell 2.3.1 read on disk (Apache-2.0 OR MIT), plus xterm.js 6.0.0, Hyper and ink read at source. The decisive finding is negative: the plugin's `src/process/mod.rs` gives every child `Stdio::piped()` for stdin/stdout/stderr and never opens a pty, so there is no TTY, no cols/rows, no SIGWINCH, and no Ctrl-C — a real terminal emulator would have nothing to emulate. So this mock drops xterm.js entirely and copies ink's architecture instead: a permanently committed scrollback region plus a small live input line, laid-out styled text rather than a VT state machine. The scrollback is a `<div role="log">` of per-line text nodes with a tabular-nums seconds gutter, matching kaleo's own ActivityFeed (which caps at MAX_LINES = 200 for the same anti-jank reason). Unverified: xterm.js's real bundle cost, and Claude Code's own renderer (not open source) — the ink-style architecture is an inference from ink, not a reading of Claude Code.

### What the real source contradicted

- The hand-rolled mock assumed a terminal. src/process/mod.rs sets stdout/stdin/stderr to Stdio::piped() and opens no pty, so the child sees isatty=false: no colour, no cols/rows, no SIGWINCH, and Child.kill() is a process kill, not Ctrl-C. The strip is now labelled 'command runner' with a literal 'pipes · no tty' chip instead of promising a shell.
- The prompt line used to show a binary name (`git status`). In the real plugin, ShellScope::_prepare does `self.scopes.iter().find(|s| s.name == command_name)` — the first argument to Command.create is the scope entry's `name` field, not a path. The mock now types scope names (`export-gerbers`, `drc-report`) and shows `git` struck through with the real error, ProgramNotAllowed.
- No xterm.js. Its default renderer is DomRenderer (CoreBrowserTerminal.ts line 33 import, `_createRenderer()` at 667–668), it emits absolutely-positioned per-cell spans, ships its own xterm.css, and themes through an ITheme object rather than CSS variables — none of which the overlay's glass/pill styling can reach into. And the canvas fallback people cite is gone: addons/ at master (6.0.0) has addon-webgl but no addon-canvas.
- No WebGL renderer, and not just because this page is static: the addon allocates a WebGL context plus texture atlas per instance, and contexts are a limited per-browser resource that can be lost — pure cost for a five-line log on an always-on-top overlay.
- node-pty is out. It is a Node native addon; Hyper can only use it because Electron bundles Node and it runs `electron-rebuild -f -o node-pty`. A shipped Tauri v2 binary has no Node runtime, so that path means a Rust pty crate and a custom command — I did not verify which crate, so none is named.
- The env row is new. `default_env()` returns Some(HashMap::default()) and commands.rs does `if let Some(env) = options.env { command.envs(env) } else { command.env_clear() }` — envs({}) is a no-op over std::process::Command, so the plugin's own '// by default we don't add any env variables' comment is misleading and the child inherits everything. Hyper hits the same class of bug and hard-deletes GOOGLE_API_KEY in app/session.ts; the mock therefore surfaces `env: explicit` as visible state, not a hidden default.
- The arg validator shown is anchored and narrow. ScopeAllowedArg::Var builds `format!("^{validator}$")` unless `"raw": true`, so the docs' own `sh -c` example with `{"validator": "\\S+"}` compiles to ^\S+$ and rejects anything containing a space. The mock drops `sh -c` entirely and spawns the target binary with a positional arg list instead — the second rejection line shows a real per-position validator failing.
- Scrollback is now capped and rendered as text nodes. Output is untrusted bytes from a child process, and kaleo's own ActivityFeed already caps at MAX_LINES = 200 — the mock states that cap on the strip rather than implying infinite history.
- HOUSE RULES OVERRODE the source projects twice: (a) Hyper and xterm.js both theme from the OS/user theme — kaleo's palette is pinned, so the identical dark glass strip sits on both the pcbnew and eeschema grounds unchanged; (b) a real terminal always shows a prompt you can hit Enter on, but here the run control is the single filled paid element and it is ABSENT, not greyed, in the eeschema frame where nothing valid is typed and the engine has never been probed — that frame shows the third engine state (dashed hollow dot, 'engine unknown'), which no terminal emulator models.
- Colour is not the only carrier on the stream tags: out/err/run/end each carry a word plus a distinct left rule (dotted, double, solid), so the log still parses in a screenshot with hue removed.

### Build note

Two prerequisites are missing in kaleo today.

(1) Rust side: `tauri-plugin-shell = "2.3.1"` is already in app/src-tauri/Cargo.toml and registered at src/lib.rs:51, but BOTH app/src-tauri/capabilities/default.json (platforms macOS) and app/src-tauri/capabilities/cross-platform.json (windows, linux) grant only `"shell:allow-open"` — add `shell:allow-spawn` (plus `shell:allow-kill`, and `shell:allow-stdin-write` only if you truly need to feed stdin) to BOTH files, or the feature silently does not exist on one platform. Use the object form kaleo already uses for `fs:allow-read-file`: `{"identifier": "shell:allow-spawn", "allow": [{"name": "drc-report", "cmd": "kicad-cli", "args": ["pcb", "drc", {"validator": "[A-Za-z0-9._-]+"}], "sidecar": false}]}` — the JSON key is `cmd` (serde rename of `command`), and it is required unless `sidecar` is true. Never write `"args": true`: that deserializes to `Flag(true)` → `args = None` → every argument list accepted verbatim, i.e. an unrestricted child process reachable from the webview. Omit `args` and only a zero-arg call is allowed.

(2) JS side: `@tauri-apps/plugin-shell` is absent from app/package.json and app/node_modules/@tauri-apps/ — install it and pin the npm version that pairs with crate 2.3.1 at install time. Then use `Command.create('drc-report', ['pcb','drc','board.kicad_pcb'], {env: {PATH: process.env.PATH}}).spawn()` — `program` is the scope entry's `name`, NOT a binary path (`self.scopes.iter().find(|s| s.name == command_name)`), and the explicit `env` is mandatory: `default_env()` returns `Some({})` and `envs({})` is a no-op, so the child otherwise inherits kaleo's entire environment including every API key it loaded. Subscribe with `cmd.stdout.on('data')`, `cmd.stderr.on('data')`, `cmd.on('close')`, `cmd.on('error')` and push each line as a text node into a capped array (cap it like ActivityFeed's MAX_LINES = 200); never `dangerouslySetInnerHTML` child output. Prefer `.spawn()` over `.execute()` so a slow command streams instead of blocking the overlay. Do not add `shell:allow-open` expectations here — it gates only `open`, and kaleo's tauri.conf.json has `"plugins": {}` so that path would be denied anyway.

### Unverified

- Claude Code's own renderer was never read — it is not open source — so "a Claude-Code-style CLI paints ANSI to stdout" is an architectural inference drawn from ink, not from Claude Code.
- xterm.js's bundle cost was never measured, so the weight argument against it is unbenchmarked.
- No Rust pty crate was evaluated, so the cost of building a real pty path instead of the piped-stdio runner is unknown and no crate is recommended.
- The npm version of @tauri-apps/plugin-shell that pairs with crate 2.3.1 was never confirmed — match it at install time rather than trusting a version here.
- Whether Tauri's $TEMP/$HOME path variables expand inside a shell scope's cmd field was not tested, only inferred from the parse call in the plugin.
- What happens when a caller passes more arguments than the scope lists was not tested; the code read has no length check, so extra arguments appear to be dropped silently.

---

## Follow KiCad

**Read:** Hammerspoon/hammerspoon (MIT) · sindresorhus/get-windows (MIT) · dimusic/active-win-pos-rs (MIT OR Apache-2.0) · ayangweb/tauri-plugin-macos-permissions (MIT) · madsmtm/objc2 — objc2-app-kit (MIT) · tauri-apps/plugins-workspace store (Apache-2.0 OR MIT) · KiCad 10.0.6 bundle metadata (GPL-3.0)

Built on Hammerspoon's application watcher (extensions/application/libapplication_watcher.m), which observes NSWorkspaceDidActivateApplicationNotification on [[NSWorkspace sharedWorkspace] notificationCenter] and pulls the NSRunningApplication out of userInfo under NSWorkspaceApplicationKey — not on active-win's snapshot API, which both sindresorhus/get-windows and dimusic/active-win-pos-rs implement as a one-shot CGWindowListCopyWindowInfo walk that a caller inevitably puts on a setInterval. The bundle identifiers in the mock were read off the installed KiCad 10.0.6 bundle with PlistBuddy: pcbnew and eeschema really are separate .app bundles with separate ids, which is why no window title and therefore no Screen Recording permission is needed in the common case. Two things here are honestly unverified: nobody launched KiCad to confirm whether opening a board from the project manager reports org.kicad.kicad or org.kicad.pcbnew (the in-process claim is inferred from ~500 KB stubs sitting over 70–96 MB .kiface modules in Contents/PlugIns/), and the actual window title strings are unknown — so the second stage shows an explicit "pane unknown" state instead of a title pattern. The 180 ms settle is our own number, not from any source.

### What the real source contradicted

- A hand-rolled mock would show a poll interval ('checks every 500 ms'). The real event-driven source is Hammerspoon's observer on [[NSWorkspace sharedWorkspace] notificationCenter] for NSWorkspaceDidActivateApplicationNotification — so the mock prints that notification name, and the only number in the row is a settle window, not a sample rate. active-win's CGWindowListCopyWindowInfo walks every on-screen window of every app per call; that is what a timer would actually cost.
- The mock originally implied a permission prompt was the price of foreground detection. It is not: active-win-pos-rs requests no permission at all and still reads the frontmost app, and in get-windows the AXIsProcessTrustedWithOptions check exists only for the AppleScript that reads a browser's active tab URL. Only kCGWindowName is TCC-gated, so the permission ledger reads NO PROMPT for the shipping path and lists Screen Recording as a separate, off-by-default title-reading row.
- Two states (pcbnew / eeschema) is wrong. KiCad ships pcbnew and eeschema as ~500 KB stubs over _pcbnew.kiface (96,523,408 bytes) and _eeschema.kiface (73,638,448 bytes) in KiCad.app/Contents/PlugIns/, so the project manager can front as org.kicad.kicad with both editors as sibling windows. The mock now carries a third explicit 'pane unknown' state — matching the house rule that there are three engine states, not two — instead of silently defaulting to the board skin.
- The identifiers are no longer invented. org.kicad.pcbnew and org.kicad.eeschema are the real CFBundleIdentifiers read off /Applications/KiCad/PCB Editor.app and Schematic Editor.app, and pcbnew.app is LSHandlerRank=Owner for .kicad_pcb — the exact path Kaleo's existing opener:allow-open-path on $HOME/**/*.kicad_pcb already takes.
- HOUSE RULES WIN over the obvious reading of the feature: 'Follow KiCad' does not reskin. One palette, pinned, that never flips with the OS theme — so the toggle changes only the scrim's ground contrast behind the strip (dark pad 42% on pcbnew's #001023, light pad 30% on eeschema's #F5F4EF) while every token inside the shell stays identical across both stages. A hand-rolled version would have swapped the whole palette per app.
- Raw activation events strobe — Cmd-Tab through a stack and the notification fires once per app. The mock states a settle window on the wire line rather than pretending the repaint is instantaneous. That debounce is our own invention, not something any of the read sources implements.
- No window-title string is shown anywhere. The researcher never launched KiCad and never read a real kCGWindowName, so hard-coding 'board.kicad_pcb — PCB Editor' into the mock would have been fabrication; the unknown state is labelled as unknown instead.
- Persistence moved off localStorage. The footnote reads settings.json · autoSave 100 ms because tauri-plugin-store's StoreOptions.autoSave is documented as a 100 ms debounce by default, and because the native watcher in Rust cannot read the webview's localStorage — the mechanism dictated the storage, not app convention.
- No paid control appears in this frame at all. The settings row spends nothing, so per 'exactly one paid control per frame' and 'absent, not greyed', there is no filled element competing with the toggle.

### Build note

Rust side, in app/src-tauri/src/: objc2-app-kit 0.3.2 is already in the graph, so no new crate and no Objective-C file. Use NSWorkspace::sharedWorkspace().notificationCenter() and add an observer for the static NSWorkspaceDidActivateApplicationNotification (objc2-app-kit src/generated/NSWorkspace.rs:664; NSWorkspaceApplicationKey at :634), read bundleIdentifier() off the NSRunningApplication, debounce, and app.emit("kaleo-frontmost-changed", bundle_id). Seed the initial value from NSWorkspace::sharedWorkspace().frontmostApplication() (:298) at startup so there is a value before the first activation. Pair every addObserver with removeObserver:name:object:nil in the window's teardown — Hammerspoon does, and a live observer over a freed object is the classic crash. Do NOT reuse app/src/hooks/useReviewedInKicad.ts for this: it listens to the webview's own `blur` and knows only that you left, never where you went. Frontend: add "core:event:allow-listen" for the new event name in app/src-tauri/capabilities/default.json alongside the existing ada-wake entries. Persistence: add tauri-plugin-store (Cargo.toml + @tauri-apps/plugin-store in package.json + `.plugin(tauri_plugin_store::Builder::default().build())`), add "store:default" to capabilities/default.json, and use `await Store.load('settings.json')` / store.set('followKicad', bool) — autoSave defaults to a 100 ms debounce. Move this one key off the localStorage path in app/src/contexts/theme.context.tsx so the Rust watcher can read the toggle without a round trip and so a webview data reset does not lose it. Fallback path only for org.kicad.kicad: tauri-plugin-macos-permissions is already a macOS dep and "macos-permissions:default" is already in capabilities — gate it behind "plugin:macos-permissions|check_screen_recording_permission", and only call request_screen_recording_permission from an explicit user click on that off-by-default row, never at launch.

### Unverified

- KiCad was never launched, so whether opening a board from the project manager reports org.kicad.kicad or org.kicad.pcbnew is unknown — the "hosts both editors in one process" claim is inferred from small stub binaries sitting over large .kiface modules in the bundle.
- The real pcbnew and eeschema window title strings were never read, so any title-matching fallback must be checked against a live kCGWindowName before it is written.
- Whether the two editors ever get separate, title-distinguishable windows inside one process on macOS is untested.
- No open-source overlay that reskins itself per foreground app was found, so this pattern has no working precedent that was actually read — Hammerspoon supplies only the watcher.
- The store plugin's Rust side and its on-disk JSON format were never read; the API and the 100 ms autoSave come from its JS wrapper and docs, and the README's store.load() shape looks stale against the current index.ts.
- The macOS version boundary for the Screen Recording prompt comes from get-windows' readme and was not confirmed on any specific release, including this machine's.

### Numbers with no source

- the 180 ms settle window printed on the activation wire line in both Follow KiCad stages — the engineer stated outright that it is its own number, implemented by none of the sources read

---

## Notifications

**Read:** tauri-apps/plugins-workspace — plugins/notification (MIT OR Apache-2.0) · hoodie/notify-rust (MIT OR Apache-2.0) · h4llow3En/mac-notification-sys (MIT) · atuinsh/desktop (Apache-2.0)

Built from the real tauri-plugin-notification v2 source (crate 2.4.0) and Atuin Desktop's NotificationManager. The gate shape — a master boolean plus a tri-state `os: "always" | "not_focused" | "never"` sub-option resolved by `!document.hasFocus()` — is Atuin's, with its default inverted from true to false. Everything the mock says about delivery is read off the plugin: `desktop.rs` hardcodes both `permission_state()` and `request_permission()` to `Granted`, passes `"com.apple.Terminal"` as the bundle id whenever `tauri::is_dev()`, and fires `let _ = notification.show()` inside a detached task, so no click and no failure ever comes back. The researcher read the source but did not run it; whether the deprecated NSUserNotificationCenter path still delivers on current macOS is unverified, and the plugin's issue tracker has open "notifications not working on macOS" reports nobody opened.

### What the real source contradicted

- A hand-rolled mock shows a permission dialog and an 'Allow / Deny' step. There is no dialog: desktop.rs is verbatim `pub fn request_permission(&self) -> crate::Result<PermissionState> { Ok(PermissionState::Granted) }`, and the code path uses the deprecated NSUserNotificationCenter, not UNUserNotificationCenter, so `requestAuthorization(options:)` is never invoked. The permission UI was deleted; the only gate drawn is Kaleo's own stored boolean.
- The mock previously read as `if (await isPermissionGranted()) send(...)`. That guard can never fail on desktop — it returns true unconditionally — so the mock now states the fact in the OFF stage's footnote and shows shouldNotify() short-circuiting at the master flag instead.
- The banner is branded 'Terminal', not 'Kaleo'. desktop.rs passes `notify_rust::set_application(if tauri::is_dev() { "com.apple.Terminal" } else { &self.identifier })`, and mac-notification-sys's setApplication returns NO unless LSCopyApplicationURLsForBundleIdentifier finds a registered bundle — the swizzle then falls back to `@"com.apple.Terminal"` anyway. A mock showing the app's own name and icon in dev is a lie about what a tester will see.
- A per-notification custom icon was dropped from the design. NSUserNotification shows the bundle's icon; the `icon` field is passed to notify_rust but not honored on macOS the way it is on Windows/Linux.
- Action buttons and a 'click to open the run' affordance were removed. `generate_handler!` in lib.rs registers only notify / request_permission / is_permission_granted on desktop; registerActionTypes, onAction, cancel, pending and channels are exported from JS and covered by permissions but hit unknown commands. mac-notification-sys does report contentsClicked (activationType 2), but the plugin discards the NotificationHandle inside `tauri::async_runtime::spawn(async move { let _ = notification.show(); })`, so no click reaches JS. The struck-through permission chips in the mock encode this.
- No error, retry, or 'delivery failed' state is drawn, because none is observable: `sendNotification()` returns void, and the Rust side discards the Result on a detached task. A failed notification is silent to both layers.
- The capability chip row shows three strings, not `notification:default`. The researcher's expansion of default is 16 permissions; only three commands exist on desktop.
- Atuin's master default is `?? true` (settings.ts:184). HOUSE RULE WINS over the source project: Kaleo's default is false, because the OFF stage's whole point is that never calling sendNotification() is what keeps macOS from registering the app in System Settings > Notifications before the user opts in.
- HOUSE RULES over the source's habits elsewhere: no paid control appears in either frame (nothing is typed, so it is absent rather than greyed), the enabled/disabled distinction is carried by knob shape — square-left vs round-filled-right — and by solid vs dashed connector and border, never by hue, and the selected tri-state segment uses a caret glyph plus an inset underline rather than a colour fill.

### Build note

Add `tauri-plugin-notification = "2.4"` to /Users/patliu/Desktop/Coding/kaleo/app/src-tauri/Cargo.toml and `.plugin(tauri_plugin_notification::init())` to the builder; `npm i @tauri-apps/plugin-notification`. Add exactly three permission strings — `"notification:allow-notify"`, `"notification:allow-is-permission-granted"`, `"notification:allow-request-permission"` — to BOTH src-tauri/capabilities/default.json (macOS) and cross-platform.json (Windows/Linux); adding to one only fails at runtime as an unknown-command rejection, not at build. Never grant `notification:default` (16 permissions for 3 commands). Write src/lib/notify/settings.ts as a copy of src/lib/speech/settings.ts — same `safeLocalStorage` + `OFF_VALUES` shape, new `KALEO_STORAGE_KEYS.NOTIFY_ENABLED` in src/config/kaleo.constants.ts, but `NOTIFY_DEFAULT_ENABLED = false`. Gate in a pure `shouldNotify()` that returns false on the master flag before anything imports the plugin, and keep the setting in a ref refreshed on toggle rather than re-reading per event (the overlay's run-finish path is hot). `sendNotification()` is synchronous and returns void — do not await it, do not wrap it in try/catch expecting signal. Only title/body/icon/sound survive to macOS. Before shipping, `grep -rn "new Notification" src/`: the plugin's js_init_script replaces `window.Notification` globally, so any stray construction posts a real OS notification and registers the app behind your gate. Repo currently has zero notification code — nothing to migrate.

### Unverified

- Nobody ran the plugin: whether the deprecated NSUserNotificationCenter path still delivers a banner on current macOS is unknown, and the open "notifications not working on macOS" issues were seen only as search-result titles.
- The "Terminal" branding shown in the mock depends on an untested condition — whether an unsigned, locally built .app is reliably registered with Launch Services at all.
- That macOS registers the app in System Settings on the first post, and that a user who previously disabled it causes a silent no-op, is a read of standard macOS behavior rather than anything confirmed in source or by test.
- tauri::is_dev() was never read, only its call site, so exactly which builds get the com.apple.Terminal bundle id is an assumption.
- The npm version of @tauri-apps/plugin-notification that pairs with crate 2.4.0 was not checked.
- Kaleo contains no notification code, plugin, permission or dependency today, so every statement in this section comes from upstream source rather than from anything in this repo.

---

## Licence ledger

Twenty-four distinct third-party repositories were actually opened and read at source level (26 rows, since three are separate plugins within tauri-apps/plugins-workspace); beyond those, one citation is documentation only, two are artifacts read off the local disk rather than projects consulted, and one — node-pty — was never opened past its package.json, while two genuinely-read repositories with NO licence at all (zzzze/tauri-plugin-spotlight, aguscruiz/voiceorb) were missing from the ledger entirely.

| Project | Licence | How checked | Used for |
| --- | --- | --- | --- |
| `ahkohd/tauri-macos-spotlight-example` | MIT (GitHub LICENSE) | source-read | NSPanel window layer: non-activating panel style mask, floating level, fullScreenAuxiliary / canJoinAllSpaces collection behaviour |
| `ospfranco/sol` | MIT, Copyright 2024 Oscar Franco (LICENSE confirmed) | source-read | Visual layer over the panel: 42px row geometry, NSVisualEffectView material/blendingMode/state settings |
| `tauri-apps/window-vibrancy` | Apache-2.0 detected by GitHub; repo ships LICENSE-APACHE and LICENSE-MIT, so dual MIT OR Apache-2.0 | source-read | How real behind-window blur is inserted: addSubview_positioned_relativeTo(..., NSWindowOrderingMode::Below, None) |
| `dip/cmdk` | MIT (GitHub LICENSE.md) | source-read | Bare Input primitive attribute set and the Raycast/Linear clone stylesheet values (640px root, 15px/8px 16px input) |
| `tauri-apps/plugins-workspace — plugins/notification (crate 2.4.0)` | Apache-2.0 OR MIT (SPDX headers in every file; workspace Cargo.toml). GitHub's repo-level detection reports only Apache-2.0. | source-read | desktop.rs hardcoding Granted permission, com.apple.Terminal bundle id under is_dev(), fire-and-forget show() |
| `hoodie/notify-rust` | MIT OR Apache-2.0 (crates.io licence field, v4.18.0) | source-read | macOS show() cfg blocks behind the plugin |
| `h4llow3En/mac-notification-sys` | MIT/Apache-2.0 (crates.io licence field, v0.6.15); GitHub detects Apache-2.0 | source-read | Thin NSUserNotification wrapper (objc/notify.m) — the deprecated delivery path |
| `atuinsh/desktop` | Apache-2.0 (GitHub LICENSE) | source-read | NotificationManager gate shape: master boolean plus tri-state os: always / not_focused / never resolved by !document.hasFocus() |
| `elevenlabs/packages — convai-widget-core` | MIT, 'Copyright (c) 2025 ElevenLabs' (LICENSE fetched) | source-read | Orb.ts resting frame: 4-stop grayscale ramp, seven polar ovals at softness 0.4, sharpRing/smoothRing constants, shipped colors #2792DC / #9CE6E6 |
| `livekit/components-js` | Apache-2.0 for the repository (GitHub LICENSE). packages/shadcn/components/agents-ui/agent-audio-visualizer-aura.tsx carries its own header: 'Licensed under the Polyform Non-Resale License 1.0.0 ... © 2026 UNCRN LLC'. Its WebGL host react-shader-toy.tsx is MIT. | source-read | use-agent-audio-visualizer-aura.ts state table (speed/scale/amplitude/frequency/brightness, 0.5s easeOut) and useTrackVolume RMS math |
| `openai/openai-realtime-console` | MIT, 'Copyright (c) 2024 OpenAI' (LICENSE fetched) | source-read | wavtools analyser/amplitude path on the websockets branch |
| `kopiro/siriwave` | MIT, 'Copyright (c) 2020 Flavio Maria De Stefano' (LICENSE fetched) | source-read | iOS9 curve constants; classic-curve.ts was downloaded but not read, so its constants are not used |
| `iamsrikanthnani/pluely (vendored into kaleo/app)` | GPL-3.0 — CONFIRMED on the upstream GitHub repo, and app/LICENSE is the verbatim GPL-3.0 text | local-artifact | Window architecture of the overlay bar: CSS-only glass on a transparent window, DragButton grip, 54→58 collapsed-height lineage |
| `ahkohd/tauri-nspanel` | Apache-2.0 (GitHub-detected LICENSE). NOTE: the v2 README text says 'MIT or MIT/Apache 2.0 where applicable' — the repo's own two statements disagree. | source-read | PanelLevel match arms; the crate Kaleo already depends on by git branch |
| `sohzm/cheating-daddy` | GPL-3.0 (GitHub LICENSE) | source-read | Contrastive: Electron's -webkit-app-region:drag cascades to the whole header, unlike Tauri's data-tauri-drag-region — this contrast is quoted in plain.json's changed_from_handrolled |
| `tauri-apps/tauri — config JSON schema + @tauri-apps/api typings` | Apache-2.0 OR MIT (node_modules/@tauri-apps/api ships LICENSE_APACHE-2.0 and LICENSE_MIT) | docs-read | Authoritative key names and defaults for WindowConfig/WindowEffectsConfig; setEffects/clearEffects/setIgnoreCursorEvents surface |
| `Hammerspoon/hammerspoon` | MIT, 'Copyright (c) 2014-2025' (LICENSE fetched) | source-read | extensions/application/libapplication_watcher.m: NSWorkspaceDidActivateApplicationNotification observer, NSRunningApplication out of NSWorkspaceApplicationKey |
| `sindresorhus/get-windows` | MIT, 'Copyright (c) Sindre Sorhus' (LICENSE fetched) | source-read | Contrastive: the one-shot CGWindowListCopyWindowInfo snapshot API that a caller inevitably polls on setInterval |
| `dimusic/active-win-pos-rs` | MIT OR Apache-2.0 (Cargo.toml; LICENSE-MIT + LICENSE-APACHE present). GitHub detects only Apache-2.0. | source-read | Same contrastive snapshot-API point, on the Rust side |
| `ayangweb/tauri-plugin-macos-permissions` | MIT, 'Copyright (c) 2024 ayang' (LICENSE fetched) | source-read | Accessibility/Screen Recording permission checks; already a Kaleo dependency |
| `madsmtm/objc2 — objc2-app-kit 0.3.2` | Zlib OR Apache-2.0 OR MIT (crates.io licence field, v0.3.2) | source-read | AppKit bindings used by the watcher path |
| `tauri-apps/plugins-workspace — plugins/store` | Apache-2.0 OR MIT (workspace Cargo.toml, inherited via license.workspace = true) | source-read | Settings persistence API surface |
| `tauri-apps/plugins-workspace — tauri-plugin-shell 2.3.1` | Apache-2.0 OR MIT (LICENSE_APACHE-2.0 + LICENSE_MIT ship in the crate; Cargo.toml license field) | source-read | The decisive negative finding: src/process/mod.rs gives every child Stdio::piped() and never opens a pty — no TTY, no cols/rows, no SIGWINCH, no Ctrl-C |
| `xtermjs/xterm.js (@xterm/xterm 6.0.0)` | MIT — xterm.js authors 2017-2019, SourceLair 2014-2016, Christopher Jeffrey 2012-2013 (LICENSE fetched) | source-read | Cited to justify dropping it: with no pty there is nothing for a VT emulator to emulate |
| `vercel/hyper` | MIT (GitHub LICENSE; package.json "license": "MIT") | source-read | Comparable terminal architecture |
| `vadimdemedes/ink` | MIT (GitHub LICENSE) | source-read | The architecture actually copied: committed scrollback region plus a small live input line, laid-out styled text rather than a VT state machine |
| `zzzze/tauri-plugin-spotlight` | NO LICENSE DETECTED (GitHub API license field is null) — all rights reserved; must not be copied | source-read | src/config.rs and src/spotlight_macos/spotlight.rs read for panel technique |
| `aguscruiz/voiceorb` | NONE — GitHub reports license: null and there is no LICENSE file; all rights reserved | source-read | app.js shader, analyser and animate() sections read as a technique reference only |
| `microsoft/node-pty` | package.json declares MIT, but GitHub's detection returns NOASSERTION — the repo carries three separate copyright holders (Christopher Jeffrey 2012-2015, Daniel Imms 2016, Microsoft 2018-present) | unverified | Named as the thing a real pty would require |
| `KiCad 10.0.6 application bundle (installed at /Applications/KiCad)` | n/a for this row. GPL-3.0 is the licence of KiCad's SOURCE, which was never consulted; an Info.plist read with PlistBuddy carries no such grant and confers nothing. | local-artifact | Bundle identifiers proving pcbnew and eeschema are separate .app bundles — hence no window title and no Screen Recording permission in the common case |

### Dropped from the ledger

- Duplicate row: ahkohd/tauri-macos-spotlight-example was counted twice by the naive split because it appears in both light.json's and plain.json's sources_line. It is one repository, merged into one row.
- '28 projects opened' as a headline number: the split conflated three distinct plugins inside the single repository tauri-apps/plugins-workspace (notification, store, shell) with three separate projects. They are kept as three rows because they are distinct code units, but they are one repo for any 'projects opened' count.
- openai/openai-realtime-agents and vercel/ai-chatbot: correctly never reached a sources_line — only recursive file listings were read to verify the ABSENCE of visualizer code. Nothing was derived; they are not provenance.
- tauri-apps/tauri v2 docs site (term researcher): a single WebFetch of v2.tauri.app/plugin/shell/ used only to cross-check a struct already read on disk. Not an independent source.
- macos-accessibility-client 0.0.1: a transitive dependency of tauri-plugin-macos-permissions, read on disk but load-bearing for nothing in the document. Dropped as noise rather than counted as a project consulted.
- kaleo / silkscreen / vendor/mudriknow: the repository under work and its own vendored code. Not third-party provenance and should never have been counted alongside external sources.
- pickle-com/glass, alexanderqchen/orb-ui, OrbitingBucket/voice-orb-visualizer, sandner-art/Audio-Shader-Studio, and all Rive/Lottie orb candidates: surfaced in search, never opened. The researchers explicitly refused to cite them, and that refusal was correct — no row.
- Two numbers that appear in the document but have no source at all: the 180 ms settle in settings.json (stated in-line as 'our own number') and xterm.js's '~250KB' bundle cost (a recollection). Neither is a provenance row, but neither should be presented as sourced.
