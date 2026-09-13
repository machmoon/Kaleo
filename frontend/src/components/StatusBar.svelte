<script>
  import { countBySeverity } from '../lib/severity.js'
  import { formatCount, joinDot } from '../lib/format.js'
  import { log } from '../lib/log.js'

  let {
    findings = null,
    reviewed = true,
    tab = 'review',
    schematicEnabled = false,
    placementEnabled = false,
    boardEnabled = false,
    caseEnabled = false,
    sourcingEnabled = false,
    debugOpen = false,
    ondebug = null,
  } = $props()

  // Read off the store rather than passed down: the counts change on their own
  // schedule, and App has no reason to re-render for a console line.
  const logErrors = $derived($log.entries.filter((e) => e.level === 'error').length)
  const logWarnings = $derived($log.entries.filter((e) => e.level === 'warn').length)

  const count = $derived(findings ? findings.length : 0)
  const counts = $derived(countBySeverity(findings || []))

  // "Review · 0" would read as a clean review; a skipped one has to say so.
  const reviewTab = $derived(
    findings ? (reviewed ? `Review · ${count}` : 'Review · not run') : 'Review',
  )

  const breakdown = $derived(
    findings && findings.length
      ? joinDot([
          counts.blocker ? formatCount(counts.blocker, 'blocker') : '',
          counts.marginal ? `${counts.marginal} marginal` : '',
          counts.note ? formatCount(counts.note, 'note') : '',
        ])
      : '',
  )
</script>

<footer class="bar" data-testid="status-bar" data-material="chrome">
  <a
    class="lbl tab"
    class:current={tab === 'chat'}
    href="#chat"
    aria-current={tab === 'chat' ? 'page' : undefined}
    data-testid="status-bar-tab-chat"
  >Chat</a>
  <!-- Tabs are hash fragments, so a switch never reaches the server. Each
       drawing wakes up only when the current run carries its data contract. -->
  {#if schematicEnabled}
    <a
      class="lbl tab"
      class:current={tab === 'schematic'}
      href="#schematic"
      aria-current={tab === 'schematic' ? 'page' : undefined}
      data-testid="status-bar-tab-schematic"
      data-enabled="true"
    >Schematic</a>
  {:else}
    <span
      class="lbl tab"
      aria-disabled="true"
      title="Run a board to see its schematic"
      data-testid="status-bar-tab-schematic"
      data-enabled="false"
    >Schematic</span>
  {/if}
  {#if placementEnabled}
    <a
      class="lbl tab"
      class:current={tab === 'placement'}
      href="#placement"
      aria-current={tab === 'placement' ? 'page' : undefined}
      data-testid="status-bar-tab-placement"
      data-enabled="true"
    >Placement</a>
  {:else}
    <span class="lbl tab" aria-disabled="true" title="Enable verified placement for this run" data-testid="status-bar-tab-placement" data-enabled="false">Placement</span>
  {/if}
  {#if boardEnabled}
    <a
      class="lbl tab"
      class:current={tab === 'board'}
      href="#board"
      aria-current={tab === 'board' ? 'page' : undefined}
      data-testid="status-bar-tab-board"
      data-enabled="true"
    >Board</a>
  {:else}
    <span class="lbl tab" aria-disabled="true" title="Run a board to see it placed" data-testid="status-bar-tab-board" data-enabled="false">Board</span>
  {/if}
  <!-- Enabled once any run finished: a run without a case still gets the tab,
       whose honest empty state explains why nothing is there. -->
  {#if caseEnabled}
    <a
      class="lbl tab"
      class:current={tab === 'case'}
      href="#case"
      aria-current={tab === 'case' ? 'page' : undefined}
      data-testid="status-bar-tab-case"
      data-enabled="true"
    >Case</a>
  {:else}
    <span class="lbl tab" aria-disabled="true" title="Run a board to see its case" data-testid="status-bar-tab-case" data-enabled="false">Case</span>
  {/if}
  <!-- Same rule as Case: a finished run without a BOM still gets the tab and
       its empty state says whether sourcing was skipped or failed. -->
  {#if sourcingEnabled}
    <a
      class="lbl tab"
      class:current={tab === 'sourcing'}
      href="#sourcing"
      aria-current={tab === 'sourcing' ? 'page' : undefined}
      data-testid="status-bar-tab-sourcing"
      data-enabled="true"
    >Sourcing</a>
  {:else}
    <span class="lbl tab" aria-disabled="true" title="Run a board to see its bill of materials" data-testid="status-bar-tab-sourcing" data-enabled="false">Sourcing</span>
  {/if}
  <a
    class="lbl tab"
    class:current={tab === 'review'}
    href="#review"
    aria-current={tab === 'review' ? 'page' : undefined}
    data-testid="status-bar-tab-review"
  >{reviewTab}</a>
  <div class="spacer"></div>
  {#if breakdown}<span class="mono breakdown" data-testid="status-bar-breakdown">{breakdown}</span>{/if}
  <!-- A disclosure, not a tab: it opens a drawer below this bar rather than
       changing what the centre column shows. Absent unless App wires it up. -->
  {#if ondebug}
    <button
      type="button"
      id="debug-console-toggle"
      class="lbl tab debug"
      aria-expanded={debugOpen}
      aria-controls="debug-console"
      onclick={ondebug}
      data-testid="status-bar-debug-toggle"
    >
      Console
      <!-- A standing "· 0" would read as a count worth watching. -->
      {#if logErrors + logWarnings > 0}
        <span class="count" class:alert={logErrors > 0} data-testid="status-bar-debug-count">· {logErrors + logWarnings}</span>
      {/if}
    </button>
  {/if}
</footer>

<style>
  .bar {
    height: var(--statusbar-h);
    border-top: 1px solid var(--rule);
    background: var(--well);
    display: flex;
    align-items: center;
    flex-shrink: 0;
  }

  .tab {
    display: flex;
    align-items: center;
    height: 100%;
    padding: 0 14px;
    border-right: 1px solid var(--rule-soft);
    color: var(--ink-faint);
    text-decoration: none;
  }
  .tab[aria-disabled='true'] { cursor: default; }
  .current { color: var(--ink); }
  .current:hover { color: var(--ink); }

  .spacer { flex-grow: 1; }
  .breakdown { font-size: var(--fs-lbl); color: var(--ink-soft); padding: 0 16px 0 0; }

  /* The tab rule, hung off the other edge of the bar. */
  .debug {
    gap: 6px;
    border: none;
    border-left: 1px solid var(--rule-soft);
    background: transparent;
  }
  /* The global ring sits 1 px outside the element, which a 28 px bar clips. */
  .debug:focus-visible { outline-offset: -2px; }
  .debug:hover { color: var(--ink); }
  .count { color: var(--ink-soft); }
  .count.alert { color: var(--sev-blocker-fg); }
</style>
