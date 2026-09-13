<script>
  import { onMount } from 'svelte'
  import { get } from 'svelte/store'
  import ActivityCard from './ActivityCard.svelte'
  import ArtifactCards from './ArtifactCards.svelte'
  import IntentForm from './IntentForm.svelte'
  import MicButton from './MicButton.svelte'
  import { listModels } from '../lib/api.js'
  import { appendTranscript } from '../lib/voice.js'
  import { downloadText } from '../lib/download.js'
  import { restoreRun, run } from '../lib/run.js'
  import {
    SESSION_MIME,
    parseSession,
    serializeSession,
    sessionFilename,
  } from '../lib/session.js'

  let {
    onsubmit,
    onclarify,
    onretry,
    onedit,
    onnew,
    onopen,
    schematicEnabled = false,
    placementEnabled = false,
    boardEnabled = false,
    reviewed = true,
  } = $props()

  let answer = $state('')
  let file = $state(null)
  let importError = $state('')
  let models = $state([])
  let placementCapabilities = $state({})
  let selectedModel = $state('auto')
  let selectedThinkingLevel = $state('auto')
  let selectedQuotaRpm = $state('auto')
  let copied = $state(false)

  const entries = $derived($run.entries || [])

  onMount(async () => {
    try {
      const catalog = await listModels()
      models = catalog.models
      placementCapabilities = catalog.placement
    } catch {
      models = []
      placementCapabilities = {}
    }
  })

  $effect(() => {
    selectedModel = $run.orchestratorModel || 'auto'
    selectedThinkingLevel = $run.thinkingLevel || 'auto'
    selectedQuotaRpm = String($run.quotaRpm || 'auto')
  })

  function clarify(event) {
    event.preventDefault()
    const text = answer.trim()
    if (!text) return
    answer = ''
    onclarify?.(text)
  }

  function save() {
    const state = get(run)
    downloadText(serializeSession(state), sessionFilename(state), SESSION_MIME)
  }

  async function openFile(event) {
    importError = ''
    const selected = event.currentTarget.files?.[0]
    event.currentTarget.value = ''
    if (!selected) return
    try {
      restoreRun(parseSession(await selected.text()))
    } catch (error) {
      importError = String(error?.message ?? error)
    }
  }

  async function copyError() {
    try {
      await navigator.clipboard.writeText(String($run.error?.message ?? $run.error ?? ''))
      copied = true
      setTimeout(() => (copied = false), 1200)
    } catch {
      copied = false
    }
  }
</script>

<section class="conversation" data-testid="conversation-view">
  <div class="sessionbar">
    <span class="lbl">Session</span>
    <span class="model mono">
      model · {$run.actualModel || 'Auto'} · {$run.actualThinkingLevel || $run.thinkingLevel || 'auto'} thinking
      {#if $run.quotaRpm !== 'auto'} · {$run.quotaRpm} RPM pace{/if}
    </span>
    <span class="spacer"></span>
    {#if entries.length}<button type="button" onclick={save}>Save session</button>{/if}
    <button type="button" onclick={() => file?.click()}>Open session</button>
    <input bind:this={file} class="file" type="file" accept=".json,application/json" onchange={openFile} />
  </div>
  {#if importError}<p class="import-error" role="alert">{importError}</p>{/if}

  {#if !entries.length}
    <IntentForm
      {onsubmit}
      initial={$run.request}
      {models}
      initialModel={$run.orchestratorModel || 'gemini-3.7-flash'}
      initialThinkingLevel={$run.thinkingLevel || 'auto'}
      initialQuotaRpm={$run.quotaRpm || 'auto'}
      {placementCapabilities}
    />
  {:else}
    <div class="thread" aria-live="polite">
      {#each entries as entry (entry.id)}
        {#if entry.type === 'message'}
          <article class="message" class:user={entry.role === 'user'} class:assistant={entry.role === 'assistant'}>
            <div class="avatar mono" data-material="tint">{entry.role === 'user' ? 'YOU' : 'AI'}</div>
            <div class="message-body" data-material={entry.role === 'user' ? 'panel' : undefined}>
              <div class="role lbl">{entry.role === 'user' ? 'You' : 'Hardy'}</div>
              <p>{entry.text}</p>
              {#if entry.result}
                <ArtifactCards
                  result={entry.result}
                  {schematicEnabled}
                  {placementEnabled}
                  {boardEnabled}
                  {reviewed}
                  {onopen}
                />
              {/if}
            </div>
          </article>
        {:else if entry.type === 'activity'}
          <ActivityCard {entry} />
        {/if}
      {/each}
    </div>

    {#if $run.phase === 'clarification'}
      <form class="reply" onsubmit={clarify} data-material="sticky">
        <label class="lbl" for="clarification">Your clarification</label>
        <div>
          <textarea id="clarification" rows="2" bind:value={answer} placeholder="Add the missing electrical constraint…" data-material="tint"></textarea>
          <MicButton context="clarify" onappend={(text) => (answer = appendTranscript(answer, text))} />
          <button type="submit" disabled={!answer.trim()}>Send</button>
        </div>
      </form>
    {:else if $run.phase === 'error'}
      {@const kind = $run.error?.kind || 'internal'}
      <section class="recovery" data-testid="chat-recovery" data-material="panel" data-kind={kind}>
        {#if kind === 'no-api-key'}
          <!-- Not an outage: an unkeyed clone is the ordinary first run. Setup
               instructions come first, and there is no retry button — retrying
               without the key cannot succeed. -->
          <div class="lbl">Hardy has no Gemini key yet</div>
          <p>
            The engine and the placer run without any key, but reading datasheets and proposing a
            circuit go through Gemini. Set the key where the service can see it, then restart it.
          </p>
          <ol class="steps" data-testid="chat-recovery-steps">
            <li><code class="mono" data-material="tint">cp .env.example .env</code></li>
            <li>Put your key in the new file as <code class="mono" data-material="tint">GOOGLE_API_KEY=…</code></li>
            <li>Restart the service so it picks the key up.</li>
          </ol>
          <p class="note">
            The <strong>Install</strong> section of the repo README covers where the key comes from
            and which parts of the pipeline need it.
          </p>
          <div class="actions">
            <button type="button" onclick={onedit}>Back to the form</button>
          </div>
        {:else}
          {#if kind === 'network'}
            <div class="lbl">The service is not running</div>
            <p>
              The browser could not reach it at all. Start it with
              <code class="mono" data-material="tint">PORT=8081 python -m service.app</code> and try again.
            </p>
          {:else}
            <div class="lbl">Run failed</div>
            <p>{$run.error?.message || 'The run did not complete.'}</p>
          {/if}
          <div class="actions">
            <button type="button" class="primary" onclick={() => onretry?.(selectedModel, selectedThinkingLevel, selectedQuotaRpm)}>Retry run</button>
            <button type="button" onclick={onedit}>Edit request</button>
            <button type="button" onclick={copyError}>{copied ? 'Copied' : 'Copy error'}</button>
            <select bind:value={selectedModel} aria-label="Retry model">
              <option value="auto">Auto model</option>
              {#each models as model (model.id)}
                <option value={model.id}>{model.name || model.id}</option>
              {/each}
            </select>
            <select bind:value={selectedThinkingLevel} aria-label="Retry reasoning effort">
              <option value="auto">Auto effort</option>
              <option value="low">Fast · low</option>
              <option value="medium">Standard · medium</option>
              <option value="high">Deep · high</option>
            </select>
            <select bind:value={selectedQuotaRpm} aria-label="Retry request pace">
              <option value="auto">Auto pace</option>
              <option value="15">15 RPM</option>
              <option value="6">6 RPM · demo-safe</option>
              <option value="3">3 RPM · conservative</option>
            </select>
            <button type="button" onclick={() => onretry?.(selectedModel, selectedThinkingLevel, selectedQuotaRpm)}>Switch settings and retry</button>
          </div>
        {/if}
      </section>
    {:else if $run.phase === 'done'}
      <div class="done-actions">
        <button type="button" onclick={onnew}>New board</button>
        <button type="button" onclick={save}>Save session JSON</button>
      </div>
    {/if}
  {/if}
</section>

<style>
  .conversation { width: min(100%, 900px); margin: 0 auto; }
  .sessionbar { display: flex; align-items: center; gap: 9px; min-height: 30px; padding-bottom: 12px; border-bottom: 1px solid var(--rule-soft); }
  .sessionbar .spacer { flex-grow: 1; }
  .sessionbar button, .done-actions button, .actions button, .actions select { padding: 5px 9px; background: transparent; border: 1px solid var(--rule); color: var(--ink-mid); font-size: 11px; }
  .model { font-size: var(--fs-mono-sm); color: var(--ink-faint); }
  .file { display: none; }
  .import-error { margin-top: 10px; color: var(--sev-blocker-fg); font-size: var(--fs-ui); }
  .thread { display: flex; flex-direction: column; gap: 16px; padding: 22px 0 30px; }
  .message { display: flex; gap: 12px; max-width: var(--measure-detail); }
  .message.user { align-self: flex-end; flex-direction: row-reverse; max-width: 78%; }
  .avatar { width: 32px; height: 32px; display: grid; place-items: center; flex-shrink: 0; border: 1px solid var(--rule); background: var(--surface); color: var(--ink-soft); font-size: 9px; }
  .user .avatar { background: var(--well); }
  .message-body { min-width: 0; flex-grow: 1; }
  .user .message-body { padding: 10px 12px; background: var(--well); border: 1px solid var(--rule-soft); }
  .role { margin-bottom: 6px; }
  .message p { white-space: pre-wrap; overflow-wrap: anywhere; color: var(--ink); font-size: var(--fs-body); line-height: 1.6; }
  .reply { position: sticky; bottom: 0; padding: 12px 0 3px; background: var(--sticky-surface); border-top: 1px solid var(--rule); }
  .reply > div { display: flex; gap: 8px; margin-top: 7px; }
  .reply textarea { min-width: 0; flex-grow: 1; resize: vertical; padding: 10px 11px; background: var(--surface); border: 1px solid var(--rule); line-height: 1.5; }
  .reply button, .primary { padding: 0 16px; border: 0; background: var(--accent); color: var(--accent-ink); }
  .reply button:disabled { background: var(--accent-off); color: var(--accent-off-ink); }
  .recovery { margin: 0 0 24px 44px; padding: 13px; max-width: var(--measure-detail); border: 1px solid var(--sev-blocker-rule); background: var(--sev-blocker-bg); }
  .recovery p { margin-top: 7px; color: var(--ink-mid); line-height: 1.5; overflow-wrap: anywhere; }
  .recovery .steps { margin: 12px 0 0; padding-left: 20px; display: flex; flex-direction: column; gap: 8px; font-size: var(--fs-ui); color: var(--ink-mid); line-height: 1.6; }
  .recovery code { background: var(--well); padding: 2px 6px; font-size: var(--fs-mono); }
  .recovery .note { margin-top: 12px; font-size: var(--fs-ui); color: var(--ink-soft); line-height: 1.6; }
  .actions { display: flex; gap: 7px; flex-wrap: wrap; align-items: center; margin-top: 12px; }
  .actions .primary { border-color: var(--accent); background: var(--accent); color: var(--accent-ink); }
  .done-actions { display: flex; gap: 8px; margin: 0 0 22px 44px; }
  @media (max-width: 720px) { .message.user { max-width: 92%; } .sessionbar .model { display: none; } }
</style>
