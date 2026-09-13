<script>
  import { get } from 'svelte/store'
  import BoardWell from './components/BoardWell.svelte'
  import CaseTab from './components/CaseTab.svelte'
  import ConversationView from './components/ConversationView.svelte'
  import DebugConsole from './components/DebugConsole.svelte'
  import GuidePointer from './components/GuidePointer.svelte'
  import PlacementLab from './components/PlacementLab.svelte'
  import PlacementResults from './components/PlacementResults.svelte'
  import ReviewResults from './components/ReviewResults.svelte'
  import SchematicWell from './components/SchematicWell.svelte'
  import SideRail from './components/SideRail.svelte'
  import SourcingTab from './components/SourcingTab.svelte'
  import StatusBar from './components/StatusBar.svelte'
  import TitleBar from './components/TitleBar.svelte'
  import { ApiError, chatStream, normalizeRequest } from './lib/api.js'
  import { highlightRefs, readPlacements } from './lib/board.js'
  import { readEnclosure } from './lib/enclosure.js'
  import { readPlacementRepair } from './lib/placement.js'
  import { reviewStatus } from './lib/review.js'
  import { applyDeskResolve, resetGuide } from './lib/guide.js'
  import { pcbText } from './lib/download.js'
  import { logEvent } from './lib/log.js'
  import {
    failRun,
    finishClarification,
    finishRun,
    resetRun,
    run,
    stageEvent,
    startRun,
  } from './lib/run.js'
  import { highlightSchematicParts, readSchematic } from './lib/schematic.js'
  import { readRouting } from './lib/routing.js'
  import { readSourcing } from './lib/sourcing.js'
  import { resolveTab } from './lib/tabs.js'

  // The tab lives in the hash fragment; App is the only thing that reads it,
  // and the status bar's links are what write it.
  let hash = $state(window.location.hash)
  let selected = $state(-1)
  let debugOpen = $state(false)
  const placementMode = new URLSearchParams(window.location.search).get('mode') === 'placement'

  $effect(() => {
    const onhash = () => {
      hash = window.location.hash
    }
    window.addEventListener('hashchange', onhash)
    return () => window.removeEventListener('hashchange', onhash)
  })

  $effect(() => {
    const onDesk = (event) => {
      applyDeskResolve(event.detail)
    }
    window.addEventListener('silkscreen:desk', onDesk)
    return () => window.removeEventListener('silkscreen:desk', onDesk)
  })

  async function submit(raw, options = {}) {
    const request = normalizeRequest(raw)
    selected = -1
    // Silently, not as a dismissal: the findings a running guide pointed
    // into are about to be replaced, and its targets with them.
    resetGuide()
    startRun(request, {
      preserve: options.preserve === true,
      message: options.message ?? request.intent,
      model: options.model,
      thinkingLevel: options.thinkingLevel,
      quotaRpm: options.quotaRpm,
    })
    goTab('chat')
    try {
      const state = get(run)
      const outcome = await chatStream(
        {
          ...request,
          clarification: String(options.clarification ?? ''),
          session_id: state.sessionId,
          turn_id: state.id,
          model: state.orchestratorModel,
          thinking_level: state.thinkingLevel,
          quota_rpm: state.quotaRpm,
        },
        stageEvent,
      )
      if (outcome.needsClarification) {
        finishClarification(outcome)
      } else if (outcome.result) {
        finishRun(outcome.result)
      } else {
        throw new ApiError('internal', 'The orchestrator finished without a board result.')
      }
    } catch (err) {
      failRun(err instanceof ApiError ? err : new ApiError('internal', String(err)))
    }
  }

  // The review is optional, so the chrome has to know whether one happened
  // before it labels anything "checked".
  //
  // Asked is not the same as answered. This read the *request* alone, so a
  // review that ran and failed still counted as reviewed and the chrome drew
  // "Review · 0", "0 findings" and "Nets checked" -- the empty finding list
  // of a critic that answered nothing, presented as a clean board. That is
  // the one thing `reviewStatus` exists to prevent, and it was used only in
  // the Review tab. `voice.js` got it right, so the app read "not known" out
  // loud while the screen said zero.
  const reviewed = $derived.by(() => {
    const requested = $run.request ? $run.request.review !== false : true
    if (!$run.result) return requested
    const status = reviewStatus($run.result)
    // `absent` is an older result with no review block at all: nothing was
    // established either way, so fall back to what was asked for.
    if (status === 'absent') return requested
    return status === 'ok'
  })

  const placements = $derived($run.phase === 'done' ? readPlacements($run.result) : null)
  const schematic = $derived($run.phase === 'done' ? readSchematic($run.result) : null)
  const placement = $derived(
    $run.phase === 'done' ? readPlacementRepair($run.result) : null,
  )
  const enclosure = $derived($run.phase === 'done' ? readEnclosure($run.result) : null)
  // null is "not routed", which the board and review surfaces say out loud.
  const routing = $derived($run.phase === 'done' ? readRouting($run.result) : null)
  const boardEnabled = $derived(placements !== null)
  const schematicEnabled = $derived(schematic !== null)
  const placementEnabled = $derived(placement !== null)
  // Enabled whenever a run finished, with or without a case: `enclosure: null`
  // is the contract's honest degradation, and the tab says so rather than
  // greying out and hiding that anything was skipped or failed.
  const caseEnabled = $derived($run.phase === 'done')
  // Sourcing follows the same rule: `sourcing: null` is the honest degradation
  // and the tab's empty state says whether it was skipped or failed.
  const sourcing = $derived($run.phase === 'done' ? readSourcing($run.result) : null)
  const sourcingEnabled = $derived($run.phase === 'done')
  const tab = $derived(
    resolveTab(hash, {
      schematic: schematicEnabled,
      placement: placementEnabled,
      board: boardEnabled,
      case: caseEnabled,
      sourcing: sourcingEnabled,
      review: $run.phase === 'done',
    }),
  )
  const pcb = $derived($run.result ? pcbText($run.result) : '')

  const findings = $derived($run.result ? $run.result.findings : [])
  const selectedFinding = $derived(findings[selected] || null)
  // Keyed off the board's own designators, not the spec names the finding
  // is written in, and filtered against the parts actually placed — see
  // highlightRefs for why an unmatched list is worse than an empty one.
  const highlightedRefs = $derived(highlightRefs(selectedFinding, placements))
  const highlightedSchematicIds = $derived(highlightSchematicParts(selectedFinding, schematic))

  // Plain let, not $state: the effect below both reads and writes it, and a
  // reactive one would wake the effect it just settled.
  let lastTab = ''

  // Watches the resolved tab rather than the click, so a hash link typed into
  // the address bar and the board-disabled fallback are both recorded.
  $effect(() => {
    const to = tab
    if (to === lastTab) return
    const from = lastTab
    lastTab = to
    logEvent('ui.tab', `tab ${from || 'none'} → ${to}`, {
      from,
      to,
      schematic_enabled: schematicEnabled,
      board_enabled: boardEnabled,
    })
  })

  function goTab(name) {
    window.location.hash = name
    hash = `#${name}`
  }

  /** Clicking the same finding again deselects it. */
  function selectFinding(index) {
    selected = selected === index ? -1 : index
    const finding = findings[index] || null
    const on = selected === index
    logEvent('ui.finding', `finding ${index} ${on ? 'selected' : 'deselected'}`, {
      index,
      selected: on,
      severity: finding ? finding.severity : '',
      parts: finding ? finding.parts : [],
      title: finding ? finding.title : '',
    })
  }

  function showOnBoard(index) {
    selected = index
    logEvent('ui.show-on-board', `finding ${index} shown on the board`, { index })
    goTab('board')
  }

  function showOnSchematic(index) {
    selected = index
    logEvent('ui.show-on-schematic', `finding ${index} shown on the schematic`, { index })
    goTab('schematic')
  }

  function newBoard() {
    selected = -1
    resetGuide()
    logEvent('ui.new-board', 'started another board', {})
    goTab('chat')
    resetRun()
  }

  function retry(
    model = $run.orchestratorModel || 'auto',
    thinkingLevel = $run.thinkingLevel || 'auto',
    quotaRpm = $run.quotaRpm || 'auto',
  ) {
    if (!$run.request) return
    // Logged before the submit, which mints the next run id: the id worth
    // recording here is the run being retried.
    logEvent('ui.retry', `retrying run ${$run.id}`, { id: $run.id })
    submit($run.request, {
      preserve: true,
      message: `Retrying: ${$run.request.intent}`,
      model,
      thinkingLevel,
      quotaRpm,
    })
  }

  function clarify(answer) {
    if (!$run.request) return
    submit($run.request, {
      preserve: true,
      message: answer,
      clarification: answer,
      model: $run.orchestratorModel || 'auto',
      thinkingLevel: $run.thinkingLevel || 'auto',
      quotaRpm: $run.quotaRpm || 'auto',
    })
  }

  function editRequest() {
    goTab('chat')
    resetGuide()
    resetRun()
  }
</script>

{#if placementMode}
  <PlacementLab />
{:else}
<div class="app" data-testid="app-root">
  <TitleBar intent={$run.request ? $run.request.intent : ''} result={$run.result} />

  <div class="body" data-testid="app-body">
    <main class="centre" data-testid="app-main">
      {#if tab === 'schematic' && schematic}
          <div class="hint" data-testid="app-hint">
            {#if selectedFinding}
              <span class="lbl">{highlightedSchematicIds.length ? 'showing' : 'no schematic part for'}</span>
              <span class="hint-title" data-testid="app-hint-title">{selectedFinding.title}</span>
              <button type="button" class="clear" data-testid="app-clear-selection" onclick={() => (selected = -1)}>
                Clear selection
              </button>
            {:else}
              <span class="lbl">validated connections · select a finding in the review to highlight one</span>
            {/if}
          </div>
          <SchematicWell {schematic} highlightedIds={highlightedSchematicIds} />
      {:else if tab === 'board' && placements}
          <div class="hint" data-testid="app-hint">
            {#if selectedFinding}
              <span class="lbl">{highlightedRefs.length ? 'showing' : 'no placed part for'}</span>
              <span class="hint-title" data-testid="app-hint-title">{selectedFinding.title}</span>
              <button type="button" class="clear" data-testid="app-clear-selection" onclick={() => (selected = -1)}>
                Clear selection
              </button>
            {:else}
              <span class="lbl">every part · select a finding in the review to highlight one</span>
            {/if}
          </div>
          <BoardWell {placements} {highlightedRefs} {pcb} {routing} />
      {:else if tab === 'placement' && placement}
          <PlacementResults {placement} />
      {:else if tab === 'case' && caseEnabled}
          <CaseTab
            {enclosure}
            stage={$run.stages?.enclosure || null}
            requested={$run.request?.enclosure === true}
          />
      {:else if tab === 'sourcing' && sourcingEnabled}
          <SourcingTab
            {sourcing}
            stage={$run.stages?.sourcing || null}
            requested={$run.request?.sourcing === true}
          />
      {:else if tab === 'review' && $run.phase === 'done' && $run.result}
          <ReviewResults
            result={$run.result}
            request={$run.request}
            onnew={newBoard}
            {selected}
            {schematicEnabled}
            {boardEnabled}
            onselect={selectFinding}
            onshowschematic={showOnSchematic}
            onshowboard={showOnBoard}
          />
      {:else}
        <ConversationView
          onsubmit={submit}
          onclarify={clarify}
          onretry={retry}
          onedit={editRequest}
          onnew={newBoard}
          onopen={goTab}
          {schematicEnabled}
          {placementEnabled}
          {boardEnabled}
          {reviewed}
        />
      {/if}
    </main>

    <SideRail result={$run.result} {reviewed} />
  </div>

  <!-- The guided pointer's overlay. Always mounted, renders nothing while
       idle; a step that names a tab switches through goTab like any other
       navigation, so the hash stays the single tab authority. -->
  <GuidePointer ongoto={goTab} />

  <!-- A push drawer between the body and the bar, not an overlay: it takes its
       height out of the layout, so nothing is ever hidden behind it. -->
  {#if debugOpen}
    <DebugConsole onclose={() => (debugOpen = false)} />
  {/if}

  <StatusBar
    findings={$run.result ? $run.result.findings : null}
    {reviewed}
    {tab}
    {schematicEnabled}
    {placementEnabled}
    {boardEnabled}
    {caseEnabled}
    {sourcingEnabled}
    {debugOpen}
    ondebug={() => (debugOpen = !debugOpen)}
  />
</div>
{/if}

<style>
  .app { height: 100%; display: flex; flex-direction: column; overflow: hidden; }
  .body { flex-grow: 1; display: flex; min-height: 0; }
  .centre { flex-grow: 1; min-width: 0; padding: 26px 34px; overflow-y: auto; }

  .hint {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 12px;
    min-height: 28px;
  }
  .hint-title {
    font-size: var(--fs-ui);
    color: var(--ink);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .clear {
    font-size: 12px;
    padding: 4px 11px;
    background: transparent;
    color: var(--ink-mid);
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    white-space: nowrap;
  }
</style>
