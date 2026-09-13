# Vendored third-party code

Code in this directory was **not written for Silkscreen**. It holds read-only
references for features described in `DEVPOST.md` or `TODO.txt`. Nothing in
`engine/`, `service/`, `scripts/`, or `frontend/` imports from here, and nothing
here runs in CI.

## mudriknow/

| | |
|---|---|
| Upstream | https://github.com/abdallahmagdy15/mudriknow |
| Revision | `ad58192` |
| License | MIT — see `mudriknow/LICENSE`, retained unmodified |
| Stack | Electron, TypeScript, React, robotjs, Windows UI Automation |
| Platform | Windows only (macOS/Linux on the upstream roadmap) |

Why it is here: its auto-guide mode is the feature Silkscreen wants — an on-screen
pointer that lands on each target with a speech bubble and walks a user through a
multi-step task. The technique is to read the active window's accessibility tree
via Windows UI Automation, pair it with a screenshot carrying a coordinate grid,
and have the model emit guide actions that position the overlay. Applying that to
KiCad is how "add a net class" becomes something you watch once and can then do
yourself.

The MIT licence permits use, modification and redistribution provided the
copyright notice is kept. It has been kept. Upstream copyright remains with the
MudrikNow contributors.

## openwhispr/

| | |
|---|---|
| Upstream | https://github.com/OpenWhispr/openwhispr |
| Revision | `abfaf2b` (`abfaf2ba010a7a122a70eb7ec4541324eebaf33d`) |
| License | MIT — see `openwhispr/LICENSE`, retained unmodified |
| Stack | Electron, React, whisper.cpp, native key/paste helpers per OS |
| Platform | macOS, Windows, Linux desktop |

Why it is here: it is a working local-first dictation app, vendored as the
reference for the desktop/local-Whisper dictation path that the shipped voice
feature deliberately does not attempt. The SPA's voice input uses the browser's
own Web Speech API — `frontend/src/lib/voice.js` — which is the honest
browser-scoped version: no model download, no audio leaves the page except
through the browser's own recognition service, and Firefox gets a notice
instead of a button. (The desktop overlay in `app/` has its own push-to-talk
path through the service's `/transcribe` route; that is not this either.)
OpenWhispr shows the other half — hold-a-hotkey dictation anywhere on the
desktop, transcribed by a local whisper.cpp model, pasted into the focused app —
plus the practical machinery that path needs (model download and management,
VAD, per-OS paste injection). None of its code is imported or repackaged; it is
read, not run. `.git` was stripped on copy.

The MIT licence permits use, modification and redistribution provided the
copyright notice is kept. It has been kept. Upstream copyright remains with
the OpenWhispr Team.

## openscad/ — NOT vendored, never committed

| | |
|---|---|
| Upstream | https://github.com/openscad/openscad |
| Revision | none — this directory is **gitignored** and holds no committed code |
| License | GPL-2.0 (with the OpenCSG/CGAL exceptions upstream documents) |
| Stack | C++, CGAL, Qt |
| Role | read-only local reference for the AI CAD enclosure feature |

Unlike `mudriknow/`, OpenSCAD is **not** part of this repository. `vendor/openscad/`
is listed in `.gitignore`; if a clone exists on your machine it is yours alone, made
for reading, and must never be committed, built, linked against, or imported from.
GPL-2.0 code stays outside this MIT project. To make a local reference clone:

```bash
git clone --depth 1 https://github.com/openscad/openscad vendor/openscad
```

The only interaction the project has with OpenSCAD is exec-ing a **user-installed
binary** (`openscad` on PATH) from `engine/silkscreen/enclosure/render.py` to render
locally-gated STL/PNG previews — the same arms-length boundary the SPICE layer keeps
with ngspice. The emitted `.scad` text is produced by our own Python; no OpenSCAD
code runs on the service path or ships in the Docker image. See
`docs/ai-cad-plan.md` (decision 4) and the disclosure in `DEVPOST.md`.
