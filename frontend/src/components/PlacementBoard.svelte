<script>
  import MetricHelp from './MetricHelp.svelte'

  let { board, label = '', score = null } = $props()

  const hardExplanation = 'Illegal geometry measured in millimetres: board-boundary, component-clearance, and keepout penetration. It must reach 0 for a legal placement.'
  const softExplanation = 'Preference cost for grouping, connector access, compactness, and thermal spacing. Lower is better, but it can never override a hard rule.'

  function size(component) {
    return component.angle === 90 || component.angle === 270
      ? [component.height, component.width]
      : [component.width, component.height]
  }
</script>

<section class="panel" data-testid="placement-board" data-label={label} data-material="panel">
  <div class="head">
    <div>
      <div class="lbl">{label}</div>
      {#if score}
        <div class="metrics" aria-label="Placement scores">
          <span><b>Hard</b><MetricHelp label="Hard score" explanation={hardExplanation} align="left" />{Number(score.hard).toFixed(3)} mm</span>
          <span><b>Soft</b><MetricHelp label="Soft score" explanation={softExplanation} />{Number(score.soft).toFixed(3)}</span>
        </div>
      {/if}
    </div>
    {#if score}<span class:legal={Number(score.hard) === 0}>{Number(score.hard) === 0 ? 'legal' : 'faulty'}</span>{/if}
  </div>

  <div class="well" data-material="canvas">
    <svg viewBox={`0 0 ${board.width} ${board.height}`} role="img" aria-label={`${label} PCB placement`}>
      <rect class="edge" x=".25" y=".25" width={board.width - 0.5} height={board.height - 0.5} />
      {#each board.keepouts || [] as keepout (keepout.name)}
        <rect
          class="keepout"
          x={keepout.x}
          y={board.height - keepout.y - keepout.height}
          width={keepout.width}
          height={keepout.height}
        />
      {/each}
      {#each board.components as component (component.ref)}
        {@const dims = size(component)}
        {@const svgY = board.height - component.y - dims[1]}
        <g data-ref={component.ref}>
          <rect
            class:fixed={component.fixed}
            class:power={component.kind === 'power' || component.kind === 'driver'}
            x={component.x}
            y={svgY}
            width={dims[0]}
            height={dims[1]}
          />
          <text x={component.x + dims[0] / 2} y={svgY + dims[1] / 2}>{component.ref}</text>
        </g>
      {/each}
    </svg>
  </div>
</section>

<style>
  .panel { min-width: 0; border: 1px solid var(--rule); background: var(--surface); }
  .head { min-height: 78px; display: flex; align-items: center; justify-content: space-between; gap: 18px; padding: 13px 16px; border-bottom: 1px solid var(--rule-soft); }
  .metrics { display: flex; flex-wrap: wrap; gap: 8px 18px; margin-top: 9px; color: var(--ink-mid); font-family: var(--font-mono); font-size: var(--fs-mono-sm); font-variant-numeric: tabular-nums; }
  .metrics > span { display: inline-flex; align-items: center; gap: 6px; }
  .metrics b { color: var(--ink); font-family: var(--font-sans); font-size: var(--fs-ui); font-weight: 650; }
  .head > span { font-family: var(--font-mono); font-size: var(--fs-lbl); text-transform: uppercase; color: var(--sev-blocker-fg); }
  .head > span.legal { color: var(--green); }
  .well { padding: clamp(14px, 2vw, 22px); background: var(--well-bg); }
  svg { display: block; width: 100%; max-height: 360px; }
  .edge { fill: var(--board-fill); stroke: var(--board-edge-cuts); stroke-width: .35; }
  g rect { fill: var(--board-component-fill); stroke: var(--board-courtyard); stroke-width: .28; }
  g rect.power { fill: var(--board-power-fill); }
  g rect.fixed { stroke: var(--board-silkscreen); stroke-width: .5; }
  text { fill: var(--board-silkscreen); font: 1.8px var(--font-mono); text-anchor: middle; dominant-baseline: middle; transform-box: fill-box; }
  .keepout { fill: var(--board-keepout-fill); stroke: var(--board-grid-dot); stroke-width: .25; stroke-dasharray: 1 1; }
</style>
