<script>
  import { routingPercent, routingStatus, routingSummary } from '../lib/routing.js'

  // `routing` is whatever readRouting() returned: null for a run that was not
  // routed, otherwise validated counts and the unrouted nets with the reasons
  // the router gave. Every word here is read off that and nothing is filled
  // in -- a board over a ratsnest names every net in it, and a run with
  // routing switched off says so instead of showing nothing.
  let { routing = null } = $props()

  const status = $derived(routingStatus(routing))
  const summary = $derived(routingSummary(routing))
  const percent = $derived(routingPercent(routing))
  const unrouted = $derived(routing ? routing.unrouted : [])
  const warnings = $derived(routing ? routing.warnings : [])
</script>

<div class="routing" data-testid="routing-summary" data-state={status}>
  <div class="line">
    <span class="lbl">Copper</span>
    <span class="mono" data-testid="routing-summary-line">{summary}</span>
    {#if percent}<span class="mono percent" data-testid="routing-summary-percent">{percent} of routable nets</span>{/if}
  </div>
  {#if status === 'unrouted'}
    <p class="note" data-testid="routing-summary-note">
      This run was submitted with routing turned off, so the board has no copper and every net
      is a ratsnest for you to route in KiCad.
    </p>
  {:else if status === 'empty'}
    <p class="note" data-testid="routing-summary-note">
      The router found no net to route, so there is nothing to report as connected.
    </p>
  {/if}
  {#if unrouted.length}
    <p class="note" data-testid="routing-summary-caveat">
      The router left {unrouted.length === 1 ? 'this net' : 'these nets'} as ratsnest. The board
      is not ready to order until they are routed by hand.
    </p>
    <ul class="unrouted" data-testid="routing-unrouted">
      {#each unrouted as entry (entry.net)}
        <li data-testid="routing-unrouted-net" data-net={entry.net}>
          <span class="mono net">{entry.net}</span>
          {#if entry.reason}<span class="reason" data-testid="routing-unrouted-reason">{entry.reason}</span>{/if}
        </li>
      {/each}
    </ul>
  {/if}
  {#if warnings.length}
    <ul class="warnings" data-testid="routing-warnings">
      {#each warnings as warning, i (i)}
        <li data-testid="routing-warning">{warning}</li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  .routing { margin-top: 12px; padding: 10px 12px; border: 1px solid var(--rule-soft); background: var(--surface); }
  .routing[data-state='partial'], .routing[data-state='unrouted'] { border-left: 3px solid var(--sev-blocker-rule); }
  .line { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; font-size: var(--fs-mono-sm); color: var(--ink); }
  .percent { color: var(--ink-soft); }
  .note { margin: 7px 0 0; font-size: var(--fs-ui); color: var(--ink-mid); line-height: 1.5; }
  .unrouted, .warnings { margin: 8px 0 0; padding-left: 0; list-style: none; display: flex; flex-direction: column; gap: 5px; }
  .unrouted li { display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap; font-size: var(--fs-ui); }
  .net { color: var(--sev-blocker-fg); font-size: var(--fs-mono-sm); }
  .reason { color: var(--ink-soft); overflow-wrap: anywhere; }
  .warnings li { font-size: var(--fs-ui); color: var(--ink-soft); }
</style>
