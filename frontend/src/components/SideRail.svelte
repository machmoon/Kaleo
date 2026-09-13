<script>
  import Icon from './Icon.svelte'
  import ConfigurationStatus from './ConfigurationStatus.svelte'
  import { countOf, joinDot } from '../lib/format.js'
  import { readRouting, routingSummary } from '../lib/routing.js'

  let { result = null, reviewed = true } = $props()

  // Every count is read off the response. The mockup's numbers were invented
  // for the artboard and must never appear here.
  const rows = $derived(
    result
      ? [
          { label: 'Parts placed', value: result.parts.length },
          {
            label: reviewed ? 'Nets checked' : 'Nets in the board',
            value: result.nets.length,
          },
          // "not routed" or "N of M nets routed, K unrouted": a rail that
          // counts nets checked and says nothing about copper reads as done.
          { label: 'Nets routed', value: routingSummary(readRouting(result)) },
          {
            label: 'Datasheet pins read',
            value: result.datasheets.reduce((n, d) => n + countOf(d.pins), 0),
          },
          {
            label: 'Requirements extracted',
            value: result.datasheets.reduce((n, d) => n + countOf(d.requirements), 0),
          },
        ]
      : [],
  )

  const sheets = $derived(
    (result ? result.datasheets : []).map((d) =>
      joinDot([d.part, d.package, countOf(d.pins) ? `${countOf(d.pins)} pins` : '']),
    ),
  )
</script>

<aside class="rail" data-testid="side-rail" data-material="chrome">
  <!-- A skipped review checked nothing, so the heading and the net row have to
       say what actually ran instead. -->
  <div class="lbl heading" data-testid="side-rail-heading">{reviewed ? 'What was checked' : 'What ran'}</div>

  {#if rows.length}
    <div class="rows" data-testid="side-rail-rows">
      {#each rows as row (row.label)}
        <div class="row" data-testid="side-rail-row" data-label={row.label}>
          <Icon name="check" size={13} />
          <span class="row-label">{row.label}</span>
          <span class="mono row-value" data-testid="side-rail-row-value">{row.value}</span>
        </div>
      {/each}
    </div>
  {:else}
    <div class="empty" data-testid="side-rail-empty">
      <strong>Request → circuit → placement</strong>
      <p>Name the chip and its job. The board, evidence, and verifier receipts appear here after the run.</p>
    </div>
  {/if}

  <div class="lbl heading ruled" data-testid="side-rail-not-checked-heading">Not checked</div>
  <p class="prose" data-testid="side-rail-not-checked-body">
    Signal integrity, EMC, thermal margins, and manufacturability at your fab. A clean review
    here is not a substitute for a human sign-off before you order boards.
  </p>

  {#if sheets.length}
    <div class="section" data-testid="side-rail-datasheets">
      <div class="lbl heading tight">Datasheets read</div>
      <div class="sheets">
        {#each sheets as sheet, i (i)}
          <div class="mono sheet" data-testid="side-rail-datasheet">{sheet}</div>
        {/each}
      </div>
    </div>
  {/if}

  <ConfigurationStatus />
</aside>

<style>
  .rail {
    width: var(--rail-w);
    border-left: 1px solid var(--rule-soft);
    background: var(--rail);
    flex-shrink: 0;
    padding: 26px 20px;
    overflow-y: auto;
  }

  .heading { margin-bottom: 14px; }
  .heading.ruled { margin-bottom: 12px; padding-top: 18px; border-top: 1px solid var(--rule-soft); }
  .heading.tight { margin-bottom: 9px; }

  .rows { display: flex; flex-direction: column; gap: 11px; margin-bottom: 26px; }
  .row { display: flex; align-items: center; gap: 9px; }
  .row-label { font-size: var(--fs-ui); flex-grow: 1; }
  .row-value { font-size: var(--fs-mono-sm); color: var(--ink-soft); }

  .empty { font-size: var(--fs-ui); color: var(--ink-faint); margin-bottom: 26px; line-height: 1.65; }
  .empty strong { color: var(--ink-mid); font-weight: 550; }
  .empty p { margin-top: 7px; }

  .prose { font-size: var(--fs-ui); color: var(--ink-soft); line-height: 1.65; }

  .section { margin-top: 26px; padding-top: 18px; border-top: 1px solid var(--rule-soft); }
  .sheets { display: flex; flex-direction: column; gap: 7px; }
  .sheet { font-size: 11px; color: var(--ink-mid); }

  @media (max-width: 760px) {
    .rail { display: none; }
  }
</style>
