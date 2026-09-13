# Design

UI mockups for Silkscreen, as Design Component artboards.

| File | Screen |
|---|---|
| `Main.dc.html` | Schematic view — the app in the **Drafting Table** direction |
| `Board.dc.html` | Board view — KiCad's dark canvas as an inset well |
| `Review.dc.html` | Design review — findings with datasheet citations |
| `Overlay.dc.html` | The floating assistant, in **Slew Rate** |
| `Teach.dc.html` | The guided cursor teaching a UI control |
| `canvas.json` | Artboard layout and annotations |

Two directions in one product. The app is **Drafting Table**: KiCad's own
`#F5F4EF` schematic paper, its oxblood/green/navy symbol colours, square
corners, Chivo + Chivo Mono. Light — which is the differentiator, since every
competitor is a dark cockpit. The assistant and the teaching cursor are
**Slew Rate**: warm stock, solder-red, silkscreen yellow, the op-amp mascot,
spring motion.

The board canvas keeps KiCad's exact values as an inset dark well — `#001023`
ground, `#C83434` F.Cu, `#4D7FC4` B.Cu, `#F2EDA1` silkscreen, `#FF26E2`
courtyard, `#00F8FF` ratsnest — mirroring KiCad's own light-schematic /
dark-board split.

These are static mockups. Nothing here is wired to the engine, with one
exception: `Review.dc.html` is built for real in `frontend/`, as a Svelte page over
`POST /generate` that the service serves at `/`.

## Rebuilding the canvas

`silkscreen-ui.html` is generated (2.4 MB — it bundles the canvas editor) and
is gitignored. Re-seed it from these sources with the `design` skill.
