<script>
  import { formatClock, formatDuration } from '../lib/format.js'
  import { elapsed } from '../lib/run.js'

  let { entry } = $props()

  const STAGES = [
    ['read', 'read datasheets'],
    ['propose', 'propose circuit'],
    ['validate', 'validate and repair'],
    ['place', 'place'],
    ['placement_repair', 'verify placement'],
    ['route', 'route copper'],
    ['review', 'review'],
    ['enclosure', 'printable case'],
    ['sourcing', 'source parts'],
  ]

  const QUIET = ['model.request', 'model.call', 'model.response']
  let open = $state({})

  function toggle(id) {
    open = { ...open, [id]: !open[id] }
  }
</script>

<section class="activity" data-testid="chat-activity" data-phase={entry.phase} data-material="panel">
  <header>
    <span class="mark" aria-hidden="true"></span>
    <div>
      <div class="title">Orchestrator activity</div>
      <div class="meta mono">
        {entry.phase === 'running' ? 'running' : entry.phase}
        <!-- The clock, not the feed, is what says "working" before the first
             stream frame arrives — the ticker only exists while running. -->
        {#if entry.phase === 'running'} · {formatDuration($elapsed / 1000)}{/if}
        {#if entry.feed?.length} · {entry.feed.length} events{/if}
      </div>
    </div>
  </header>

  <ol class="stages" aria-label="Pipeline stages">
    {#each STAGES as stage (stage[0])}
      <li data-state={entry.stages?.[stage[0]]?.state || ''}>
        <span class="box" aria-hidden="true"></span>{stage[1]}
      </li>
    {/each}
  </ol>

  {#if entry.feed?.length}
    <ol class="feed">
      {#each entry.feed as row (row.id)}
        <li class:quiet={QUIET.includes(row.event)} data-event={row.event}>
          <div class="line">
            <span class="time mono">{formatClock(row.t_s)}</span>
            {#if row.layer}<span class="layer mono">{row.layer}</span>{/if}
            <span class="copy">{row.text}</span>
            {#if row.detail}
              <button
                type="button"
                class="raw mono"
                aria-expanded={open[row.id] === true}
                onclick={() => toggle(row.id)}
              >{open[row.id] ? 'hide' : `show ${row.detailLabel || 'raw'}`}</button>
            {/if}
          </div>
          {#if row.detail && open[row.id]}
            <pre class="detail mono" data-material="tint">{row.detail}</pre>
          {/if}
        </li>
      {/each}
    </ol>
  {/if}
</section>

<style>
  .activity {
    margin-left: 44px;
    border: 1px solid var(--rule-soft);
    background: var(--surface);
    max-width: var(--measure-detail);
  }
  header { display: flex; gap: 10px; align-items: center; padding: 11px 13px; border-bottom: 1px solid var(--rule-soft); }
  .mark { width: 9px; height: 9px; border: 1px solid var(--rule); background: var(--well); }
  .activity[data-phase='running'] .mark { background: var(--navy); border-color: var(--navy); }
  .activity[data-phase='done'] .mark { background: var(--green); border-color: var(--green); }
  .activity[data-phase='error'] .mark { background: var(--sev-blocker-fg); border-color: var(--sev-blocker-fg); }
  .title { font-size: var(--fs-ui); color: var(--ink); }
  .meta { margin-top: 2px; font-size: var(--fs-mono-sm); color: var(--ink-soft); }

  .stages { list-style: none; display: flex; flex-wrap: wrap; gap: 7px 14px; margin: 0; padding: 10px 13px; border-bottom: 1px solid var(--rule-soft); }
  .stages li { display: flex; gap: 6px; align-items: center; font-size: 11px; color: var(--ink-faint); }
  .box { width: 8px; height: 8px; border: 1px solid var(--rule); }
  .stages li[data-state='running'] { color: var(--ink); }
  .stages li[data-state='running'] .box { border-color: var(--ink); }
  .stages li[data-state='done'] .box { background: var(--ink-mid); border-color: var(--ink-mid); }
  .stages li[data-state='failed'] { color: var(--sev-blocker-fg); }
  .stages li[data-state='failed'] .box { background: var(--sev-blocker-fg); border-color: var(--sev-blocker-fg); }

  .feed { list-style: none; margin: 0; padding: 0 13px; max-height: 390px; overflow: auto; }
  .feed li { padding: 7px 0; font-size: var(--fs-ui); }
  .feed li + li { border-top: 1px solid var(--rule-soft); }
  .line { display: flex; align-items: baseline; gap: 9px; }
  .time { width: 5ch; flex-shrink: 0; text-align: right; font-size: var(--fs-mono-sm); color: var(--ink-soft); }
  .layer { padding: 1px 4px; background: var(--well); color: var(--ink-soft); font-size: 9px; }
  .copy { color: var(--ink-mid); overflow-wrap: anywhere; }
  .quiet .copy, .quiet .time { color: var(--ink-faint); }
  .raw { margin-left: auto; flex-shrink: 0; border: 0; padding: 0; background: none; color: var(--navy); font-size: var(--fs-mono-sm); text-decoration: underline; text-underline-offset: 2px; }
  .detail { margin: 7px 0 2px calc(5ch + 9px); padding: 9px 11px; max-height: 280px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; background: var(--well); color: var(--ink-mid); border-left: 2px solid var(--rule); font-size: var(--fs-mono-sm); line-height: 1.5; }
</style>
