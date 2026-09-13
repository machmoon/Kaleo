<script>
  import { formatCount, joinDot } from '../lib/format.js'
  import {
    columnsFor,
    layoutSchematic,
    partRosterEntry,
  } from '../lib/schematic.js'

  let { schematic, highlightedIds = [] } = $props()

  let availableW = $state(0)
  let availableH = $state(0)

  const columns = $derived(columnsFor(availableW, schematic.parts.length))
  const drawing = $derived(layoutSchematic(schematic, columns))
  const stageWidth = $derived(Math.max(drawing.width, availableW))
  const stageHeight = $derived(Math.max(drawing.height, availableH))
  const offsetX = $derived((stageWidth - drawing.width) / 2)
  const selected = $derived(new Set(highlightedIds.map(String)))
  const anySelected = $derived(selected.size > 0)
  const caption = $derived(
    joinDot([
      formatCount(schematic.parts.length, 'part'),
      formatCount(schematic.nets.length, 'net'),
      'validated topology',
    ]),
  )
</script>

<figure class="schematic" data-testid="schematic-well">
  <div class="frame" data-material="canvas">
    <div class="lbl well-label" data-testid="schematic-well-label">Schematic</div>

    <div
      class="scroll"
      data-testid="schematic-well-scroll"
      bind:clientWidth={availableW}
      bind:clientHeight={availableH}
    >
      <div
        class="stage"
        data-testid="schematic-well-stage"
        style="width:{stageWidth}px; height:{stageHeight}px;"
      >
        <svg
          width={stageWidth}
          height={stageHeight}
          viewBox={`0 0 ${stageWidth} ${stageHeight}`}
          role="img"
          aria-label="Circuit schematic, {schematic.parts.length} parts and {schematic.nets.length} nets"
          data-testid="schematic-well-svg"
        >
          <defs>
            <pattern id="schematic-grid" width="8" height="8" patternUnits="userSpaceOnUse">
              <circle cx="1" cy="1" r="0.8" fill="var(--schematic-grid-dot)" />
            </pattern>
          </defs>
          <rect width={stageWidth} height={stageHeight} fill="url(#schematic-grid)" />

          <g transform={`translate(${offsetX} 0)`} aria-hidden="true">
            <text x="28" y="31" class="sheet-title">SCHEMATIC</text>

            {#each drawing.parts as part (part.id)}
              {@const on = selected.has(part.id)}
              {@const cx = part.x + part.width / 2}
              {@const cy = part.y + part.height / 2}
              <g
                class="part"
                class:dim={anySelected && !on}
                class:highlighted={on}
                data-testid="schematic-well-part"
                data-id={part.id}
                data-ref={part.ref || ''}
                data-kind={part.kind}
                data-highlighted={on}
              >
                <title>{partRosterEntry(part)}</title>

                {#if on}
                  <rect
                    class="halo"
                    x={part.x - 10}
                    y={part.y - 22}
                    width={part.width + 20}
                    height={part.height + 34}
                    rx="8"
                  />
                {/if}

                {#if part.kind === 'device'}
                  <rect
                    class="device-body"
                    x={part.x}
                    y={part.y}
                    width={part.width}
                    height={part.height}
                  />
                  <text class="ref" x={cx} y={part.y - 10} text-anchor="middle">{part.ref || part.id}</text>
                  <text class="value" x={cx} y={cy - (part.symbol ? 7 : 0)} text-anchor="middle">{part.value}</text>
                  {#if part.symbol}
                    <text class="symbol-name" x={cx} y={cy + 14} text-anchor="middle">{part.symbol}</text>
                  {/if}
                {:else}
                  <rect class="passive-hit" x={part.x} y={part.y} width={part.width} height={part.height} />
                  <text class="ref" x={cx} y={part.y + 17} text-anchor="middle">{part.ref || part.id}</text>
                  <text class="passive-value" x={cx} y={part.y + part.height - 10} text-anchor="middle">{part.value}</text>

                  {#if part.kind === 'resistor'}
                    <path class="symbol-wire" d={`M ${part.x} ${cy} H ${cx - 31} M ${cx + 31} ${cy} H ${part.x + part.width}`} />
                    <rect class="passive-body" x={cx - 31} y={cy - 10} width="62" height="20" />
                  {:else if part.kind === 'capacitor'}
                    <path class="symbol-wire" d={`M ${part.x} ${cy} H ${cx - 7} M ${cx + 7} ${cy} H ${part.x + part.width}`} />
                    <path class="passive-body" d={`M ${cx - 7} ${cy - 22} V ${cy + 22} M ${cx + 7} ${cy - 22} V ${cy + 22}`} />
                  {:else if part.kind === 'inductor'}
                    <path class="symbol-wire" d={`M ${part.x} ${cy} H ${cx - 40} M ${cx + 40} ${cy} H ${part.x + part.width}`} />
                    <path class="passive-body" d={`M ${cx - 40} ${cy} q 10 -22 20 0 q 10 -22 20 0 q 10 -22 20 0 q 10 -22 20 0`} />
                  {:else if part.kind === 'diode'}
                    <path class="symbol-wire" d={`M ${part.x} ${cy} H ${cx - 25} M ${cx + 25} ${cy} H ${part.x + part.width}`} />
                    <path class="passive-body" d={`M ${cx - 25} ${cy - 22} L ${cx + 16} ${cy} L ${cx - 25} ${cy + 22} Z M ${cx + 19} ${cy - 23} V ${cy + 23}`} />
                  {:else}
                    <path class="symbol-wire" d={`M ${part.x} ${cy} H ${cx - 28} M ${cx + 28} ${cy} H ${part.x + part.width}`} />
                    <path class="passive-body" d={`M ${cx - 36} ${cy - 22} V ${cy + 22} M ${cx - 28} ${cy - 16} H ${cx + 28} V ${cy + 16} H ${cx - 28} Z M ${cx + 36} ${cy - 22} V ${cy + 22}`} />
                  {/if}
                {/if}

                {#each part.pins as pin (`${part.id}:${pin.name}`)}
                  {@const midX = (pin.x + pin.wireX) / 2}
                  <line
                    class:unconnected={!pin.net}
                    class="net-wire"
                    x1={pin.x}
                    y1={pin.y}
                    x2={pin.wireX}
                    y2={pin.y}
                    data-testid="schematic-well-wire"
                    data-net={pin.net}
                  />
                  {#if pin.net}
                    <circle class="junction" cx={pin.wireX} cy={pin.y} r="2.7" />
                  {/if}
                  <text class="net-label" x={midX} y={pin.y - 7} text-anchor="middle">{pin.net || 'NC'}</text>

                  {#if part.kind === 'device'}
                    <text
                      class="pin-name"
                      x={pin.x + (pin.side === 'left' ? 8 : -8)}
                      y={pin.y + 4}
                      text-anchor={pin.side === 'left' ? 'start' : 'end'}
                    >{pin.name}</text>
                    <text
                      class="pin-number"
                      x={pin.x + (pin.side === 'left' ? -5 : 5)}
                      y={pin.y + 4}
                      text-anchor={pin.side === 'left' ? 'end' : 'start'}
                    >{pin.number}</text>
                  {/if}
                {/each}
              </g>
            {/each}
          </g>
        </svg>

        <ul class="roster" data-testid="schematic-well-roster">
          {#each drawing.parts as part (part.id)}
            <li>{partRosterEntry(part)}{selected.has(part.id) ? ', highlighted' : ''}</li>
          {/each}
        </ul>
      </div>
    </div>
  </div>

  <figcaption class="caption" data-material="panel">
    <span class="mono" data-testid="schematic-well-caption">{caption}</span>
    <span class="spacer"></span>
    <span class="mono selected-caption" data-testid="schematic-well-selection">
      {#if anySelected}{formatCount(selected.size, 'part')} highlighted{/if}
    </span>
  </figcaption>
</figure>

<style>
  .schematic { margin: 0; }

  .frame {
    position: relative;
    height: clamp(340px, 62vh, 700px);
    background: var(--paper);
    border: 1px solid var(--rule);
    box-shadow: inset 0 1px 5px color-mix(in srgb, var(--ink) 12%, transparent);
    overflow: hidden;
  }
  .well-label { position: absolute; top: 12px; left: 16px; z-index: 2; pointer-events: none; }
  .scroll { width: 100%; height: 100%; overflow: auto; }
  .stage { position: relative; min-width: 100%; min-height: 100%; }
  svg { display: block; }

  .sheet-title {
    font-family: var(--font-mono);
    font-size: 10px;
    letter-spacing: .08em;
    fill: var(--ink-soft);
  }

  .part { transition: opacity 120ms ease; }
  .part.dim { opacity: .22; }
  .halo { fill: var(--sev-blocker-bg); stroke: var(--sev-blocker-rule); stroke-width: 1.5; }

  .device-body, .passive-body {
    fill: var(--schematic-symbol-fill);
    stroke: var(--oxblood);
    stroke-width: 1.6;
  }
  .part.highlighted .device-body,
  .part.highlighted .passive-body { stroke: var(--sev-blocker-rule); stroke-width: 2.4; }
  .passive-hit { fill: transparent; stroke: none; }
  .symbol-wire { fill: none; stroke: var(--oxblood); stroke-width: 1.6; }

  .net-wire { stroke: var(--green); stroke-width: 1.6; }
  .net-wire.unconnected { stroke: var(--ink-faint); stroke-dasharray: 3 3; }
  .junction { fill: var(--green); }
  .net-label {
    font-family: var(--font-mono);
    font-size: 9px;
    fill: var(--schematic-net-label);
  }

  .ref {
    font-family: var(--font-mono);
    font-size: 12px;
    font-weight: 600;
    fill: var(--teal);
  }
  .value { font-family: var(--font-sans); font-size: 12px; font-weight: 600; fill: var(--oxblood); }
  .passive-value { font-family: var(--font-mono); font-size: 10px; fill: var(--teal); }
  .symbol-name {
    font-family: var(--font-mono);
    font-size: 8px;
    fill: var(--ink-soft);
    text-overflow: ellipsis;
  }
  .pin-name { font-family: var(--font-mono); font-size: 8.5px; fill: var(--oxblood); }
  .pin-number { font-family: var(--font-mono); font-size: 8px; fill: var(--oxblood); }

  .roster {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }

  .caption {
    min-height: 34px;
    border: 1px solid var(--rule);
    border-top: none;
    background: var(--surface);
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 7px 12px;
    color: var(--ink-soft);
    font-size: var(--fs-mono-sm);
  }
  .spacer { flex-grow: 1; }
  .selected-caption { color: var(--sev-blocker-fg); }
</style>
