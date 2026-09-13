<script>
  import { onMount } from 'svelte'
  import { getConfigurationStatus } from '../lib/api.js'

  const POLL_MS = 5000
  const labels = {
    ready: 'ready',
    off: 'optional',
    warning: 'check',
    error: 'blocked',
    restart: 'restart',
  }
  const marks = {
    ready: '✓',
    off: '○',
    warning: '!',
    error: '×',
    restart: '↻',
  }

  let status = $state(null)
  let checking = $state(false)
  let error = $state('')
  let checkedAt = $state('')
  let controller = null

  async function refresh() {
    if (checking) return
    checking = true
    error = ''
    const requestController = new AbortController()
    controller = requestController
    try {
      status = await getConfigurationStatus({ signal: requestController.signal })
      checkedAt = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    } catch {
      if (!requestController.signal.aborted) error = 'Readiness check unavailable.'
    } finally {
      checking = false
      if (controller === requestController) controller = null
    }
  }

  onMount(() => {
    refresh()
    const interval = window.setInterval(refresh, POLL_MS)
    const onVisibility = () => {
      if (document.visibilityState === 'visible') refresh()
    }
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      window.clearInterval(interval)
      document.removeEventListener('visibilitychange', onVisibility)
      controller?.abort()
    }
  })
</script>

<section class="configuration" data-testid="configuration-status">
  <div class="title-row">
    <div class="lbl">Backend readiness</div>
    <button
      type="button"
      class="refresh mono"
      onclick={refresh}
      disabled={checking}
      aria-label="Refresh backend readiness"
      title="Refresh backend readiness"
    >
      {checking ? 'checking' : 'refresh'}
    </button>
  </div>

  {#if status}
    <div class="live mono"><span class="live-dot"></span>live · 5 s{checkedAt ? ` · ${checkedAt}` : ''}</div>

    <div class="item dotenv" class:attention={status.dotenv.state === 'restart'} data-state={status.dotenv.state}>
      <div class="item-head">
        <span class="mark" aria-hidden="true">{marks[status.dotenv.state]}</span>
        <strong>.env sync</strong>
        <span class="badge mono">{labels[status.dotenv.state]}</span>
      </div>
      <p>{status.dotenv.summary}</p>
      {#if status.dotenv.pending.length}
        <div class="variables mono">{status.dotenv.pending.join(' · ')}</div>
      {/if}
    </div>

    <div class="feature-list">
      {#each status.features as feature (feature.id)}
        <div class="item" data-state={feature.state} data-testid={`configuration-${feature.id}`}>
          <div class="item-head">
            <span class="mark" aria-hidden="true">{marks[feature.state]}</span>
            <strong>{feature.label}</strong>
            <span class="badge mono">{labels[feature.state]}</span>
          </div>
          <p>{feature.summary}</p>
          <div class="variables mono">{feature.variables.join(' · ')}</div>
        </div>
      {/each}
    </div>
  {:else if error}
    <div class="monitor-error" role="status">
      <span>{error}</span>
      <button type="button" onclick={refresh}>Try again</button>
    </div>
  {:else}
    <div class="checking">Checking the running backend…</div>
  {/if}
</section>

<style>
  .configuration {
    margin-top: 26px;
    padding-top: 18px;
    border-top: 1px solid var(--rule-soft);
  }
  .title-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
  .refresh {
    padding: 0;
    border: 0;
    background: transparent;
    color: var(--ink-soft);
    font-size: 9.5px;
    text-transform: uppercase;
    letter-spacing: .05em;
  }
  .refresh:hover:not(:disabled) { color: var(--ink); }
  .refresh:disabled { color: var(--ink-faint); }
  .live {
    display: flex;
    align-items: center;
    gap: 5px;
    margin: 7px 0 12px;
    color: var(--ink-faint);
    font-size: 9.5px;
  }
  .live-dot {
    width: 5px;
    height: 5px;
    border-radius: 50%;
    background: var(--green);
    box-shadow: 0 0 0 2px color-mix(in srgb, var(--green) 16%, transparent);
  }
  .feature-list { display: flex; flex-direction: column; }
  .item {
    padding: 10px 0;
    border-top: 1px solid var(--rule-soft);
  }
  .item.dotenv { border: 1px solid var(--rule-soft); padding: 10px; margin-bottom: 3px; }
  .item.dotenv[data-state='restart'] { border-color: var(--sev-marginal-rule); }
  .item-head { display: flex; align-items: center; gap: 7px; }
  .item-head strong { flex: 1; font-size: 11.5px; font-weight: 550; color: var(--ink-mid); }
  .mark {
    display: grid;
    place-items: center;
    width: 15px;
    height: 15px;
    border: 1px solid currentColor;
    border-radius: 50%;
    color: var(--ink-soft);
    font-family: var(--font-mono);
    font-size: 10px;
    line-height: 1;
  }
  [data-state='ready'] .mark { color: var(--green); }
  [data-state='warning'] .mark,
  [data-state='restart'] .mark { color: var(--sev-marginal-fg); }
  [data-state='error'] .mark { color: var(--sev-blocker-fg); }
  [data-state='off'] .mark { color: var(--ink-faint); }
  .badge {
    font-size: 8.5px;
    letter-spacing: .04em;
    text-transform: uppercase;
    color: var(--ink-soft);
  }
  [data-state='ready'] .badge { color: var(--green); }
  [data-state='warning'] .badge,
  [data-state='restart'] .badge { color: var(--sev-marginal-fg); }
  [data-state='error'] .badge { color: var(--sev-blocker-fg); }
  .item p {
    margin: 5px 0 0 22px;
    color: var(--ink-soft);
    font-size: 10.5px;
    line-height: 1.45;
  }
  .variables {
    margin: 5px 0 0 22px;
    color: var(--ink-faint);
    font-size: 8.5px;
    line-height: 1.45;
    overflow-wrap: anywhere;
  }
  .checking, .monitor-error {
    margin-top: 10px;
    color: var(--ink-soft);
    font-size: 10.5px;
    line-height: 1.5;
  }
  .monitor-error { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
  .monitor-error button {
    padding: 3px 7px;
    border: 1px solid var(--rule);
    background: transparent;
    color: var(--ink-mid);
    font-size: 9.5px;
  }
</style>
