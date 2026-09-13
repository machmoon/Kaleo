<script>
  import { onMount } from 'svelte'
  import MetricHelp from './MetricHelp.svelte'
  import PlacementBoard from './PlacementBoard.svelte'
  import TitleBar from './TitleBar.svelte'
  import { ApiError, listModels, repairPlacement } from '../lib/api.js'
  import { downloadText } from '../lib/download.js'

  const profiles = [
    { id: 'compact-control', name: 'Compact Control', note: 'Tight functional groups and short local connections.' },
    { id: 'thermal-first', name: 'Thermal First', note: 'Separates hot power devices before chasing density.' },
  ]

  const hardExplanation = 'Illegal geometry measured in millimetres. The verifier accepts moves that reduce this first, and the final value must be 0.'
  const softExplanation = 'The selected team profile’s preference cost. Lower is better after legality is protected; it never makes an illegal move acceptable.'
  const memoryKey = 'silkscreen-placement-feedback-v2'

  let profile = $state('compact-control')
  let policy = $state('deterministic')
  let placementCapabilities = $state({})
  let experimental = $state(false)
  let recordTrace = $state(false)
  let result = $state(null)
  let busy = $state(false)
  let error = $state('')
  let teaching = $state(false)
  let feedbackByProfile = $state({})

  const receipts = $derived(
    result
      ? result.steps.flatMap((step) =>
          (step.receipts || [])
            .map((receipt) => ({ action: receipt.action, receipt, step })),
        )
      : [],
  )
  const accepted = $derived(receipts.filter((item) => item.receipt.accepted))
  const teachRef = $derived(accepted.length ? accepted[0].action.ref : '')
  const availablePolicies = $derived(placementCapabilities?.policies || {})

  function mergeFeedback(current = {}, supplied = {}) {
    const merged = { ...current, ...supplied }
    for (const key of ['fixed_refs_add', 'edge_refs_add']) {
      const values = [...(current[key] || []), ...(supplied[key] || [])]
      if (values.length) merged[key] = [...new Set(values)]
    }
    for (const key of ['groups_add', 'thermal_pairs_add']) {
      const values = [...(current[key] || []), ...(supplied[key] || [])]
      if (values.length) {
        merged[key] = [...new Map(values.map((value) => [JSON.stringify(value), value])).values()]
      }
    }
    if (current.weights || supplied.weights) {
      merged.weights = { ...(current.weights || {}), ...(supplied.weights || {}) }
    }
    return merged
  }

  function remember(feedback) {
    const merged = mergeFeedback(feedbackByProfile[profile], feedback)
    feedbackByProfile = { ...feedbackByProfile, [profile]: merged }
    try {
      sessionStorage.setItem(memoryKey, JSON.stringify(feedbackByProfile))
    } catch {
      // Repair still works when browser storage is unavailable.
    }
    return merged
  }

  async function run(feedback = null) {
    busy = true
    error = ''
    try {
      const remembered = feedback
        ? remember(feedback)
        : feedbackByProfile[profile]
      result = await repairPlacement({
        profile,
        policy,
        experimental_placement: experimental,
        record_trace: experimental && recordTrace,
        ...(remembered ? { feedback: remembered } : {}),
      })
    } catch (err) {
      error = err instanceof ApiError ? err.message : String(err)
    } finally {
      busy = false
      teaching = false
    }
  }

  function choose(next) {
    profile = next
    run()
  }

  function teach() {
    if (!teachRef) return
    teaching = true
    run({ fixed_refs_add: [teachRef] })
  }

  function toggleExperimental() {
    experimental = !experimental
    if (!experimental) {
      policy = 'deterministic'
      recordTrace = false
    }
  }

  function save() {
    if (!result) return
    downloadText(JSON.stringify(result.board, null, 2), 'repaired-placement.json', 'application/json')
  }

  onMount(async () => {
    try {
      const saved = JSON.parse(sessionStorage.getItem(memoryKey) || '{}')
      if (saved && typeof saved === 'object' && !Array.isArray(saved)) {
        feedbackByProfile = saved
      }
    } catch {
      feedbackByProfile = {}
    }
    try {
      placementCapabilities = (await listModels()).placement
    } catch {
      placementCapabilities = {}
    }
    await run()
  })
</script>

<div class="lab" data-testid="placement-lab">
  <TitleBar intent="PCB placement agent" />
  <main>
    <header class="hero">
      <div>
        <div class="eyebrow lbl">Collaborative Partner · verifier grounded</div>
        <h1>Repair the fault. Keep the team’s judgment.</h1>
        <p>One damaged motor-control board, two valid layouts. Every move is measured by real geometry before it is accepted.</p>
      </div>
      <a href="/" class="back">Build a board</a>
    </header>

    <section class="controls" aria-label="Placement controls" data-material="panel">
      <div class="control-group">
        <div class="control-label">
          <span class="lbl">Team placement profile</span>
          <span>Choose how legal alternatives should be ranked. Corrections stay in this browser.</span>
        </div>
        <div class="profiles">
          {#each profiles as item}
            <button
              class:active={profile === item.id}
              type="button"
              aria-pressed={profile === item.id}
              onclick={() => choose(item.id)}
              disabled={busy}
            >
              <strong>{item.name}</strong><span>{item.note}</span>
            </button>
          {/each}
        </div>
      </div>
      <div class="control-group policy-group">
        <div class="control-label">
          <label class="lbl" for="policy">Proposal mode</label>
          <span>The verifier still decides every move.</span>
        </div>
        <div class="policy">
          <select id="policy" bind:value={policy} onchange={() => run()} disabled={busy}>
            <option value="deterministic">Deterministic · offline</option>
            {#if experimental}
              <option value="fast">Auto experimental</option>
              <option value="ollama" disabled={!availablePolicies.ollama}>Ollama · local</option>
              <option value="tinker" disabled={!availablePolicies.tinker}>Tinker · checkpoint</option>
              <option value="hybrid" disabled={!availablePolicies.hybrid}>Hybrid · local then Gemini</option>
            {/if}
            <option value="gemini" disabled={!availablePolicies.gemini}>Gemini directly · demo</option>
          </select>
          <button class="run" type="button" onclick={() => run()} disabled={busy}>{busy ? 'Repairing…' : 'Run repair'}</button>
        </div>
        <div class="experimental-row">
          <button
            type="button"
            class:active={experimental}
            aria-pressed={experimental}
            disabled={busy}
            onclick={toggleExperimental}
          >Experimental features · {experimental ? 'ON' : 'OFF'}</button>
          {#if experimental}
            <label><input type="checkbox" bind:checked={recordTrace} /> Record failure traces</label>
          {/if}
        </div>
      </div>
    </section>

    {#if error}
      <div class="error" role="alert">{error}</div>
    {:else if result}
      <section class="boards">
        <PlacementBoard board={result.start} score={result.score.before} label="Before · intentionally damaged" />
        <PlacementBoard board={result.board} score={result.score.after} label={`After · ${result.profile.name}`} />
      </section>

      <section class="trace" data-material="panel">
        <div class="trace-head">
          <div>
            <span class="lbl">{Math.max(0, result.score.before.violations.length - result.score.after.violations.length)} faults repaired · {result.score.after.violations.length} remaining</span>
            <h2>Verified moves</h2>
            <div class="score-key">
              <span>Hard <MetricHelp label="Hard score" explanation={hardExplanation} align="left" /></span>
              <span>Soft <MetricHelp label="Soft score" explanation={softExplanation} /></span>
            </div>
          </div>
          <div class="actions">
            {#if teachRef}
              <button type="button" onclick={teach} disabled={busy || teaching}>Reject {teachRef} move and remember</button>
            {/if}
            <button type="button" class="primary" onclick={save}>Download placement</button>
          </div>
        </div>
        <ol>
          {#each receipts as receipt, index}
            <li>
              <span class="index">{String(index + 1).padStart(2, '0')}</span>
              <div>
                <code>{receipt.action.kind} {receipt.action.ref} {Number(receipt.action.x).toFixed(1)} {Number(receipt.action.y).toFixed(1)}</code>
                <small>{receipt.step.proposer} · hard {Number(receipt.receipt.hard_before).toFixed(2)} → {Number(receipt.receipt.hard_after).toFixed(2)} mm · soft {Number(receipt.receipt.soft_before).toFixed(3)} → {Number(receipt.receipt.soft_after).toFixed(3)}</small>
              </div>
              <span class:rejected={!receipt.receipt.accepted}>{receipt.receipt.accepted ? 'accepted' : 'rejected'} · {receipt.receipt.reason}</span>
            </li>
          {:else}
            <li><span>No score-improving action was accepted.</span></li>
          {/each}
        </ol>
      </section>
    {/if}
  </main>
</div>

<style>
  .lab { min-height: 100%; background: var(--paper); }
  main { max-width: 1380px; margin: 0 auto; padding: clamp(26px, 3vw, 44px) clamp(20px, 3vw, 42px) 64px; }
  .hero { display: flex; gap: 32px; align-items: flex-start; justify-content: space-between; padding: 8px 0 30px; border-bottom: 1px solid var(--rule); }
  .eyebrow { color: var(--oxblood); margin-bottom: 10px; }
  h1 { font-size: 31px; font-weight: 580; letter-spacing: -.035em; max-width: 760px; }
  .hero p { max-width: 720px; margin-top: 11px; color: var(--ink-mid); font-size: var(--fs-body); line-height: 1.55; }
  .back { min-height: 44px; display: inline-flex; align-items: center; color: var(--ink-soft); font-family: var(--font-mono); font-size: var(--fs-mono-sm); white-space: nowrap; }
  .controls { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 36px; align-items: end; margin: 24px 0 28px; padding: 18px; border: 1px solid var(--rule-soft); background: var(--surface); }
  .control-group { min-width: 0; }
  .control-label { display: flex; align-items: baseline; gap: 12px; margin-bottom: 10px; }
  .control-label > span:last-child { color: var(--ink-soft); font-size: var(--fs-ui); }
  .profiles { display: grid; grid-template-columns: repeat(2, minmax(220px, 320px)); gap: 12px; }
  .profiles button { min-height: 72px; text-align: left; border: 1px solid var(--rule); background: var(--surface); padding: 14px 16px; }
  .profiles button.active { border-color: var(--oxblood); box-shadow: inset 3px 0 var(--oxblood); }
  .profiles strong, .profiles span { display: block; }
  .profiles strong { font-size: var(--fs-ui); }
  .profiles span { color: var(--ink-soft); font-size: var(--fs-mono-sm); line-height: 1.45; margin-top: 6px; }
  .policy { display: flex; align-items: center; gap: 10px; }
  .experimental-row { display: flex; align-items: center; gap: 10px; margin-top: 9px; color: var(--ink-soft); font-size: var(--fs-ui); }
  .experimental-row button { min-height: 32px; padding: 0 10px; border: 1px solid var(--rule); background: transparent; color: var(--ink-soft); }
  .experimental-row button.active { color: var(--navy); border-color: var(--navy); }
  .experimental-row label { display: flex; align-items: center; gap: 6px; }
  select, .run, .actions button { min-height: 44px; border: 1px solid var(--rule); background: var(--surface); padding: 0 14px; }
  .run, .actions .primary { color: var(--accent-ink); background: var(--accent); border-color: var(--accent); }
  .boards { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
  .trace { margin-top: 24px; border: 1px solid var(--rule); background: var(--surface); }
  .trace-head { display: flex; align-items: center; justify-content: space-between; gap: 24px; padding: 18px 20px; border-bottom: 1px solid var(--rule-soft); }
  h2 { margin-top: 5px; font-size: 18px; font-weight: 550; }
  .score-key { display: flex; gap: 16px; margin-top: 10px; color: var(--ink-mid); font-size: var(--fs-ui); }
  .score-key span { display: inline-flex; align-items: center; gap: 6px; }
  .actions { display: flex; gap: 8px; }
  ol { list-style: none; margin: 0; padding: 0; }
  .trace li { min-height: 52px; display: grid; grid-template-columns: 42px 1fr auto; align-items: center; gap: 14px; padding: 11px 20px; border-bottom: 1px solid var(--rule-soft); }
  .trace li:last-child { border-bottom: 0; }
  .trace li code { color: var(--ink); font-family: var(--font-mono); }
  .trace li small { display: block; margin-top: 4px; color: var(--ink-soft); font-family: var(--font-mono); font-size: var(--fs-mono-sm); }
  .trace li > span:last-child, .index { color: var(--ink-soft); font-family: var(--font-mono); font-size: var(--fs-mono-sm); }
  .trace li > span.rejected { color: var(--sev-blocker-fg); }
  .error { padding: 18px; border: 1px solid var(--sev-blocker-rule); color: var(--sev-blocker-fg); background: var(--sev-blocker-bg); }
  @media (max-width: 940px) {
    .controls { grid-template-columns: 1fr; gap: 22px; }
    .policy-group { max-width: 540px; }
  }
  @media (max-width: 680px) {
    main { padding: 22px 16px 44px; }
    .hero { flex-direction: column; gap: 14px; padding-bottom: 22px; }
    .boards { grid-template-columns: 1fr; }
    .profiles { grid-template-columns: 1fr; width: 100%; }
    .control-label { align-items: flex-start; flex-direction: column; gap: 5px; }
    .policy { align-items: stretch; flex-direction: column; }
    select, .run { width: 100%; }
    .trace-head { align-items: flex-start; flex-direction: column; }
    .actions { width: 100%; flex-direction: column; }
    .actions button { width: 100%; }
    .trace li { grid-template-columns: 32px minmax(0, 1fr); }
    .trace li > span:last-child { grid-column: 2; }
  }
</style>
