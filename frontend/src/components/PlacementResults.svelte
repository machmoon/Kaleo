<script>
  import MetricHelp from './MetricHelp.svelte'
  import PlacementBoard from './PlacementBoard.svelte'
  import { downloadText } from '../lib/download.js'

  let { placement } = $props()
  const receipts = $derived(
    placement
      ? (placement.steps || []).flatMap((step) =>
          (step.receipts || []).map((receipt) => ({ step, receipt })),
        )
      : [],
  )
  const accepted = $derived(receipts.filter((item) => item.receipt.accepted))

  function save() {
    if (!placement?.board) return
    downloadText(
      `${JSON.stringify(placement.board, null, 2)}\n`,
      'verified-placement.json',
      'application/json',
    )
  }
</script>

{#if placement}
  <section class="placement-results" data-testid="placement-results">
    <header>
      <div>
        <span class="lbl">verifier-grounded placement</span>
        <h1>{placement.profile?.name || 'Placement profile'}</h1>
        <p>
          {placement.applied ? 'Hard geometry reached zero and the verified coordinates were used for routing.' : 'The bounded repair stopped before every verifier fault was removed, so the original CP-SAT coordinates were kept.'}
          {#if placement.policy_fallback}
            {placement.policy_fallback.from} fell back to {placement.policy_fallback.to}.
          {/if}
        </p>
      </div>
      <button type="button" onclick={save}>Download placement JSON</button>
    </header>

    <div class="summary" data-material="panel">
      <span><b>{accepted.length}</b> accepted moves</span>
      <span><b>{Number(placement.score?.before?.hard || 0).toFixed(3)}</b> → <b>{Number(placement.score?.after?.hard || 0).toFixed(3)}</b> mm hard</span>
      <span><b>{placement.policy}</b> policy</span>
      <MetricHelp label="Verifier authority" explanation="A model may propose moves, but only deterministic score improvements are written back before routing." align="right" />
    </div>

    <div class="boards">
      <PlacementBoard board={placement.start} score={placement.score?.before} label="Before · CP-SAT placement" />
      <PlacementBoard board={placement.board} score={placement.score?.after} label={`After · ${placement.profile?.name || 'verified'}`} />
    </div>

    <section class="trace" data-material="panel">
      <div class="trace-heading">
        <span class="lbl">verified move receipts</span>
        <span class="mono">{receipts.length} checked</span>
      </div>
      <ol>
        {#each receipts as item, index}
          <li>
            <span class="mono index">{String(index + 1).padStart(2, '0')}</span>
            <div>
              <code>{item.receipt.action.kind} {item.receipt.action.ref} {Number(item.receipt.action.x).toFixed(2)} {Number(item.receipt.action.y).toFixed(2)}</code>
              <small>{item.step.proposer} · hard {Number(item.receipt.hard_before).toFixed(3)} → {Number(item.receipt.hard_after).toFixed(3)}</small>
            </div>
            <span class:rejected={!item.receipt.accepted}>{item.receipt.accepted ? 'accepted' : 'rejected'}</span>
          </li>
        {:else}
          <li class="empty">The generated placement needed no accepted move.</li>
        {/each}
      </ol>
    </section>
  </section>
{/if}

<style>
  .placement-results { max-width: 1180px; margin: 0 auto; }
  header { display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; margin-bottom: 14px; }
  h1 { margin-top: 5px; font-size: var(--fs-h1); font-weight: 560; }
  header p { margin-top: 7px; max-width: 70ch; color: var(--ink-mid); line-height: 1.5; }
  header button { min-height: 38px; padding: 0 12px; border: 1px solid var(--rule); background: var(--surface); color: var(--ink-mid); white-space: nowrap; }
  .summary { display: flex; align-items: center; gap: 18px; padding: 10px 13px; border: 1px solid var(--rule-soft); background: var(--surface); color: var(--ink-mid); font-size: var(--fs-ui); }
  .summary b { color: var(--ink); font-weight: 600; }
  .summary > :last-child { margin-left: auto; }
  .boards { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; margin-top: 14px; }
  .trace { margin-top: 14px; border: 1px solid var(--rule-soft); background: var(--surface); }
  .trace-heading { display: flex; justify-content: space-between; padding: 11px 13px; border-bottom: 1px solid var(--rule-soft); }
  .trace-heading .mono { color: var(--ink-soft); font-size: var(--fs-mono-sm); }
  ol { list-style: none; margin: 0; padding: 0; }
  li { display: grid; grid-template-columns: 34px minmax(0, 1fr) auto; gap: 10px; align-items: center; padding: 9px 13px; border-bottom: 1px solid var(--rule-soft); }
  li:last-child { border-bottom: 0; }
  code { color: var(--ink); font-family: var(--font-mono); }
  small { display: block; margin-top: 3px; color: var(--ink-soft); font-family: var(--font-mono); font-size: var(--fs-mono-sm); }
  li > span { color: var(--green); font-size: var(--fs-mono-sm); }
  li > span.rejected { color: var(--sev-blocker-fg); }
  .index { color: var(--ink-faint); }
  .empty { display: block; color: var(--ink-soft); font-size: var(--fs-ui); }
  @media (max-width: 760px) {
    header { flex-direction: column; }
    .boards { grid-template-columns: 1fr; }
    .summary { align-items: flex-start; flex-direction: column; gap: 7px; }
    .summary > :last-child { margin-left: 0; }
  }
</style>
