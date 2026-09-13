<script>
  import {
    COURTYARD,
    EDGE_CUTS,
    GRID_DOT,
    GRID_DOT_OPACITY,
    SILKSCREEN,
    fitScale,
    flipTransform,
    labelTransform,
    layerCaption,
    padRects,
    partDetails,
    partLabel,
    rectAttrs,
    refFontMm,
    stagePx,
    tipPlacement,
    viewBoxOf,
    viewBoxString,
  } from '../lib/board.js'
  import { downloadPcb } from '../lib/download.js'
  import { formatBoard, formatCount, formatWirelength, joinDot } from '../lib/format.js'
  import { routingSummary } from '../lib/routing.js'
  import RoutingSummary from './RoutingSummary.svelte'

  // `placements` is whatever readPlacements() returned, so everything here is
  // already validated geometry — there is nothing left to guess about.
  // `routing` is readRouting()'s answer: null is a run that was not routed,
  // and the caption and receipt below say so rather than leaving it out.
  let { placements, highlightedRefs = [], pcb = '', routing = null } = $props()

  let availableW = $state(0)
  let availableH = $state(0)
  let hovered = $state(null)

  const box = $derived(viewBoxOf(placements))
  const scale = $derived(fitScale(box, availableW, availableH))
  const stage = $derived(stagePx(box, scale))
  const flip = $derived(flipTransform(placements.heightMm))

  // A one-millimetre drafting grid, its dots held near a pixel and a half
  // across however far in the board is zoomed.
  const gridMm = 1
  const dotR = $derived(0.8 / scale)

  const selected = $derived(new Set(highlightedRefs.map(String)))
  const anySelected = $derived(selected.size > 0)

  const caption = $derived(
    joinDot([
      formatBoard([placements.widthMm, placements.heightMm]),
      formatCount(placements.parts.length, 'part'),
      formatWirelength(placements.wirelengthMm),
      routingSummary(routing),
    ]),
  )

  // Guarded on membership: a run that replaces the board must not leave a
  // tooltip describing a part that is no longer on it.
  const tip = $derived(
    hovered && placements.parts.includes(hovered)
      ? {
          part: hovered,
          rows: partDetails(hovered),
          ...tipPlacement(hovered.courtyard_mm, box, scale, placements.heightMm, stage.width),
        }
      : null,
  )

  // The drawing is one `role="img"` node, so nothing inside it reaches a
  // screen reader and the tooltip only ever answers a pointer. This is the
  // same board as text, and it is why no rect is a tab stop.
  const roster = $derived(
    placements.parts.map((part) => ({
      text: [
        partLabel(part),
        part.footprint,
        part.value,
        selected.has(String(part.ref)) ? 'highlighted' : '',
      ]
        .filter(Boolean)
        .join(', '),
    })),
  )

  function show(part) {
    hovered = part
  }

  function hide() {
    hovered = null
  }
</script>

<figure class="board" data-testid="board-well">
  <div class="frame" data-material="canvas">
    <div class="lbl well-label" data-testid="board-well-label">{layerCaption(placements.parts)}</div>

    <div class="scroll" data-testid="board-well-scroll" bind:clientWidth={availableW} bind:clientHeight={availableH}>
      <div class="stage" data-testid="board-well-stage" style="width:{stage.width}px; height:{stage.height}px;">
        <svg
          width={stage.width}
          height={stage.height}
          viewBox={viewBoxString(box)}
          role="img"
          aria-label="Placed board, {placements.parts.length} parts"
          data-testid="board-well-svg"
        >
          <defs>
            <pattern id="board-grid" width={gridMm} height={gridMm} patternUnits="userSpaceOnUse">
              <circle cx={dotR} cy={dotR} r={dotR} fill={GRID_DOT} opacity={GRID_DOT_OPACITY} />
            </pattern>
          </defs>
          <rect
            x={box.minX}
            y={box.minY}
            width={box.width}
            height={box.height}
            fill="url(#board-grid)"
          />

          <!-- The single Y-flip. Everything below is in solver coordinates. -->
          <g transform={flip}>
            <rect
              x="0"
              y="0"
              width={placements.widthMm}
              height={placements.heightMm}
              fill="none"
              stroke={EDGE_CUTS}
              stroke-width="1.5"
              vector-effect="non-scaling-stroke"
              data-testid="board-well-outline"
            />

            {#each placements.parts as part, i (i)}
              {@const courtyard = rectAttrs(part.courtyard_mm)}
              {@const on = selected.has(String(part.ref))}
              <g class="part" data-testid="board-well-part" data-ref={part.ref} data-highlighted={on} class:dim={anySelected && !on}>
                <rect
                  {...courtyard}
                  fill="none"
                  stroke={COURTYARD}
                  stroke-width={on ? 2.2 : 1}
                  vector-effect="non-scaling-stroke"
                />
                {#each padRects(part) as pad, p (p)}
                  <rect data-testid="board-well-pad" x={pad.x} y={pad.y} width={pad.width} height={pad.height} fill={pad.color} />
                {/each}
                <text
                  class="ref"
                  transform={labelTransform(part.courtyard_mm)}
                  font-size={refFontMm(part.ref, part.courtyard_mm)}
                  fill={SILKSCREEN}
                  text-anchor="middle"
                  dominant-baseline="central"
                  data-testid="board-well-ref"
                >{part.ref}</text>
                <!-- Last, and the only thing in the group that takes a
                     pointer: an unpainted rect hit-tests nothing by default,
                     so this one asks for every event over the courtyard. It
                     is deliberately not focusable — see the part list below
                     the drawing. -->
                <rect
                  {...courtyard}
                  class="hit"
                  fill="none"
                  pointer-events="all"
                  onpointerenter={() => show(part)}
                  onpointerleave={hide}
                  data-testid="board-well-part-hit"
                  data-ref={part.ref}
                />
              </g>
            {/each}
          </g>
        </svg>

        <ul class="roster" data-testid="board-well-roster">
          {#each roster as entry, i (i)}
            <li>{entry.text}</li>
          {/each}
        </ul>

        {#if tip}
          <div class="tip" data-testid="board-well-tip" data-material="popover" class:below={tip.below} style="left:{tip.left}px; top:{tip.top}px;">
            <div class="mono tip-ref">{tip.part.ref}</div>
            <dl class="tip-rows">
              {#each tip.rows as row (row.label)}
                <dt class="lbl">{row.label}</dt>
                <dd class="mono">{row.text}</dd>
              {/each}
            </dl>
          </div>
        {/if}
      </div>
    </div>
  </div>

  <figcaption class="caption">
    <span class="mono" data-testid="board-well-caption">{caption}</span>
    <span class="spacer"></span>
    {#if pcb}
      <button type="button" class="download" data-testid="board-well-download" onclick={() => downloadPcb(pcb)}>
        Download .kicad_pcb
      </button>
    {/if}
  </figcaption>

  <RoutingSummary {routing} />
</figure>

<style>
  .board { margin: 0; }

  .frame {
    position: relative;
    height: clamp(300px, 54vh, 620px);
    background: var(--well-bg);
    border: 1px solid var(--ink-soft);
    box-shadow: inset 0 1px 6px var(--shadow-inset);
    overflow: hidden;
  }

  .well-label {
    position: absolute;
    top: 12px;
    left: 16px;
    color: var(--well-text);
    pointer-events: none;
    z-index: 1;
  }

  /* The board scrolls in here, never the page. */
  .scroll { height: 100%; overflow: auto; display: flex; }
  .stage { position: relative; margin: auto; flex-shrink: 0; }

  svg { display: block; }
  /* Off-screen but in the accessibility tree: the drawing above is a single
     image node, so this list is the only thing that names its parts. */
  .roster {
    position: absolute;
    width: 1px;
    height: 1px;
    margin: -1px;
    padding: 0;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
    list-style: none;
  }
  /* Everything drawn is inert; the hit rect above is what responds. */
  svg text, svg g > rect:not(.hit) { pointer-events: none; }
  .ref { font-family: var(--font-mono); }

  .part { transition: opacity .12s ease; }
  .dim { opacity: .22; }

  .tip {
    position: absolute;
    transform: translate(-50%, calc(-100% - 9px));
    background: var(--surface);
    border: 1px solid var(--rule);
    padding: 8px 11px 9px;
    min-width: 150px;
    max-width: 260px;
    pointer-events: none;
    box-shadow: 0 2px 8px var(--shadow-pop);
    z-index: 2;
  }
  .tip.below { transform: translate(-50%, 9px); }
  .tip-ref {
    font-size: var(--fs-mono);
    font-weight: 600;
    color: var(--ink);
    margin-bottom: 5px;
  }
  .tip-rows {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 3px 10px;
    margin: 0;
  }
  .tip-rows dd {
    margin: 0;
    font-size: var(--fs-mono-sm);
    color: var(--ink-mid);
    overflow-wrap: anywhere;
  }

  .caption {
    display: flex;
    align-items: center;
    gap: 14px;
    margin-top: 10px;
    font-size: var(--fs-mono-sm);
    color: var(--ink-soft);
  }
  .spacer { flex-grow: 1; }

  .download {
    font-size: 12px;
    padding: 6px 13px;
    background: transparent;
    color: var(--ink-mid);
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    white-space: nowrap;
  }
</style>
