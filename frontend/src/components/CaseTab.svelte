<script>
  import { downloadText } from '../lib/download.js'
  import {
    FILE_SLOTS,
    MARGIN_AXES,
    STEP_FILENAME,
    STEP_MIME,
    failedClauses,
    formatMargin,
    formatMm,
    hasCollision,
    hasKernelFailure,
  } from '../lib/enclosure.js'
  import { formatCount } from '../lib/format.js'
  import { logEvent } from '../lib/log.js'

  // `enclosure` is readEnclosure()'s output or null; `stage` is the run store's
  // enclosure stage row (so a failure can say why); `requested` says whether
  // the run asked for a case at all. Null enclosure renders an explicit state,
  // never a blank pane — the contract's degradation is honest and so is this.
  let { enclosure = null, stage = null, requested = false } = $props()

  const COPY_FLASH_MS = 1200
  let copyLabel = $state('Copy')
  let copyTimer = 0

  const collides = $derived(hasCollision(enclosure?.margins))
  const failed = $derived(!enclosure && stage?.state === 'failed')
  const kernel = $derived(enclosure?.kernel ?? null)
  const kernelFailed = $derived(hasKernelFailure(kernel))
  const failing = $derived(failedClauses(kernel))
  const writtenFiles = $derived(
    enclosure ? FILE_SLOTS.filter((slot) => enclosure.files[slot]).map((slot) => ({ slot, path: enclosure.files[slot] })) : [],
  )

  const ENGINE_LABEL = {
    kernel: 'build123d kernel (OCCT) — B-rep solids, STEP export, geometric assertions',
  }
  const SLOT_LABEL = { step: 'STEP assembly', base_stl: 'base STL', lid_stl: 'lid STL' }

  async function copy() {
    if (!enclosure) return
    try {
      await navigator.clipboard.writeText(enclosure.step)
      copyLabel = 'Copied'
    } catch {
      copyLabel = 'Copy failed'
    }
    clearTimeout(copyTimer)
    copyTimer = setTimeout(() => (copyLabel = 'Copy'), COPY_FLASH_MS)
    logEvent('ui.case-copy', 'copied the enclosure STEP assembly', {
      chars: enclosure.step.length,
    })
  }

  function save() {
    if (!enclosure) return
    downloadText(enclosure.step, STEP_FILENAME, STEP_MIME)
  }
</script>

<section class="case-tab" data-testid="case-tab">
  {#if enclosure}
    <header>
      <div>
        <span class="lbl">3d-printable enclosure</span>
        <h1>Case</h1>
        <p>
          A solid model built from the placed board's measured geometry and
          exported as a STEP assembly, alongside a printable STL per part.
          {#if enclosure.repairRounds > 0}
            Accepted after {formatCount(enclosure.repairRounds, 'repair round')}.
          {:else}
            Accepted on the first proposal.
          {/if}
        </p>
      </div>
      <div class="actions">
        <button type="button" onclick={copy} data-testid="case-copy">{copyLabel}</button>
        <button type="button" class="primary" onclick={save} data-testid="case-download">Download {STEP_FILENAME}</button>
      </div>
    </header>

    <p class="engine" data-testid="case-engine" data-engine={enclosure.engine ?? 'unknown'}>
      <span class="lbl">engine</span>
      <b>{enclosure.engine ?? 'not reported'}</b>
      {#if enclosure.engine}
        <span class="detail">{ENGINE_LABEL[enclosure.engine]}</span>
      {:else}
        <span class="detail">the response did not say which engine drew this case</span>
      {/if}
    </p>

    {#if enclosure.brief}
      <p class="brief" data-testid="case-brief"><span class="lbl">brief</span> {enclosure.brief}</p>
    {/if}

    <div class="hint" data-material="panel" data-testid="case-hint">
      <span class="lbl">see it in 3d</span>
      <p>
        Download <code>{STEP_FILENAME}</code> and open it in any CAD tool that
        reads ISO 10303-21 — FreeCAD, Fusion, Onshape, SolidWorks. The assembly
        carries the base and the lid as separately labelled solids, each in its
        assembled position. The STLs beside it are the printable pair.
      </p>
    </div>

    {#if enclosure.margins}
      <div class="receipt" data-material="panel" data-testid="case-fit" data-clean={!collides}>
        <span class="lbl">verified fit · signed margins</span>
        {#each MARGIN_AXES as axis (axis)}
          <span
            class="margin mono"
            class:collision={enclosure.margins[axis] < 0}
            data-testid="case-margin"
            data-axis={axis}
          >{axis} <b>{formatMargin(enclosure.margins[axis])} mm</b></span>
        {/each}
        {#if collides}
          <span class="verdict bad" data-testid="case-fit-verdict">negative margin — the cavity collides with the board</span>
        {:else}
          <span class="verdict" data-testid="case-fit-verdict">board clears the cavity on every axis</span>
        {/if}
      </div>
    {:else}
      <!-- A missing receipt is said out loud: an absent check must never read
           as a passed one. -->
      <div class="receipt" data-material="panel" data-testid="case-fit" data-clean="false">
        <span class="lbl">verified fit</span>
        <span class="verdict" data-testid="case-fit-verdict">no fit receipt arrived with this case — fit is unverified</span>
      </div>
    {/if}

    {#if kernel}
      <section class="kernel" data-material="panel" data-testid="case-kernel" data-passed={!kernelFailed}>
        <div class="heading">
          <span class="lbl">kernel receipt · signed margins</span>
          {#if kernelFailed}
            <span class="verdict bad" data-testid="case-kernel-verdict">
              {#if failing.length}
                {formatCount(failing.length, 'check')} failed: {failing.map((c) => c.name).join(', ')}
              {:else}
                kernel verdict failed without naming a clause
              {/if}
            </span>
          {:else if kernel.clauses.length}
            <span class="verdict" data-testid="case-kernel-verdict">
              {kernel.clauses.length} of {kernel.clauses.length} checks pass
            </span>
          {:else}
            <span class="verdict" data-testid="case-kernel-verdict">kernel ran no checks</span>
          {/if}
        </div>
        {#if kernel.clauses.length}
          <table data-testid="case-clauses">
            <tbody>
              {#each kernel.clauses as clause (clause.name)}
                <tr data-testid="case-clause" data-clause={clause.name} data-passed={clause.passed}>
                  <td class="status mono" class:fail={!clause.passed}>{clause.passed ? 'pass' : 'FAIL'}</td>
                  <td><code>{clause.name}</code></td>
                  <td class="mono value" class:fail={!clause.passed}>
                    {#if clause.marginMm === null}no margin{:else}{formatMargin(clause.marginMm)} mm{/if}
                  </td>
                  <td class="detail-cell">{clause.detail}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        {/if}
        {#if kernel.warnings.length}
          <ul class="warnings inset" data-testid="case-kernel-warnings">
            {#each kernel.warnings as warning, index (index)}
              <li data-testid="case-kernel-warning">{warning}</li>
            {/each}
          </ul>
        {/if}
      </section>
    {:else}
      <!-- No report is said out loud, the same rule as the fit receipt. -->
      <div class="receipt" data-material="panel" data-testid="case-kernel" data-passed="none">
        <span class="lbl">kernel receipt</span>
        <span class="verdict" data-testid="case-kernel-verdict">no kernel check ran on this case</span>
      </div>
    {/if}

    {#if writtenFiles.length}
      <section class="files" data-material="panel">
        <div class="heading">
          <span class="lbl">files beside the project</span>
          <span class="mono">{formatCount(writtenFiles.length, 'file')}</span>
        </div>
        <table data-testid="case-files">
          <tbody>
            {#each writtenFiles as file (file.slot)}
              <tr data-testid="case-file" data-slot={file.slot}>
                <td>{SLOT_LABEL[file.slot]}</td>
                <td class="mono path">{file.path}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </section>
    {/if}

    {#if enclosure.warnings.length}
      <ul class="warnings" data-testid="case-warnings">
        {#each enclosure.warnings as warning, index (index)}
          <li data-testid="case-warning">{warning}</li>
        {/each}
      </ul>
    {/if}

    {#if enclosure.params.length}
      <section class="params" data-material="panel">
        <div class="heading">
          <span class="lbl">emitted parameters</span>
          <span class="mono">{formatCount(enclosure.params.length, 'value')}</span>
        </div>
        <table data-testid="case-params">
          <tbody>
            {#each enclosure.params as param (param.name)}
              <tr data-testid="case-param" data-name={param.name}>
                <td><code>{param.name}</code></td>
                <td class="mono value">{formatMm(param.mm)} mm</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </section>
    {/if}

    <section class="source" data-material="panel">
      <div class="heading">
        <span class="lbl">{STEP_FILENAME}</span>
        <span class="mono">{enclosure.step.length} chars</span>
      </div>
      <pre data-testid="case-step"><code>{enclosure.step}</code></pre>
    </section>
  {:else}
    <!-- The honest empty states: failed is not skipped, skipped is not failed. -->
    <div class="empty" data-material="panel" data-testid="case-empty" data-state={failed ? 'failed' : 'absent'}>
      {#if failed}
        <span class="lbl">case generation failed</span>
        <p>
          The enclosure stage gave up{stage.error ? `: ${stage.error}` : ''}.
          The board itself was still generated and delivered — a case failure
          never fails the run.
        </p>
      {:else if requested}
        <span class="lbl">no case arrived</span>
        <p>A case was requested, but this run's response carried none.</p>
      {:else}
        <span class="lbl">no case was generated</span>
        <p>This run did not ask for an enclosure. Case generation is opt-in per run.</p>
      {/if}
    </div>
  {/if}
</section>

<style>
  .case-tab { max-width: 1180px; margin: 0 auto; }
  header { display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; margin-bottom: 14px; }
  h1 { margin-top: 5px; font-size: var(--fs-h1); font-weight: 560; }
  header p { margin-top: 7px; max-width: 70ch; color: var(--ink-mid); line-height: 1.5; }
  .actions { display: flex; gap: 8px; }
  .actions button { min-height: 38px; padding: 0 12px; border: 1px solid var(--rule); background: var(--surface); color: var(--ink-mid); white-space: nowrap; }
  .actions button:hover { color: var(--ink); }
  .actions button.primary {
    background: var(--accent);
    color: var(--accent-ink);
    border: none;
    border-radius: var(--radius);
    font-weight: 500;
  }
  .actions button.primary:hover { color: var(--accent-ink); }

  .hint {
    margin-bottom: 14px;
    padding: 12px 14px;
    border: 1px solid var(--rule);
    background: var(--surface);
  }
  .hint p { margin-top: 6px; color: var(--ink-mid); line-height: 1.5; max-width: 70ch; }

  .engine { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; margin-bottom: 10px; color: var(--ink-mid); font-size: var(--fs-ui); }
  .engine b { color: var(--ink); font-family: var(--font-mono); font-weight: 600; }
  .engine .detail { color: var(--ink-soft); }
  .brief { margin-bottom: 10px; color: var(--ink-mid); font-size: var(--fs-ui); line-height: 1.5; max-width: 80ch; }

  .kernel, .files { margin-top: 14px; border: 1px solid var(--rule-soft); background: var(--surface); }
  .kernel .verdict { margin-left: auto; }
  .status { width: 4ch; color: var(--green); font-weight: 600; }
  .fail { color: var(--sev-blocker-fg); }
  .detail-cell { color: var(--ink-mid); }
  .path { color: var(--ink-mid); word-break: break-all; }
  .warnings.inset { margin: 0; }
  .warnings.inset li { border-left: 0; border-right: 0; }
  .warnings.inset li:last-child { border-bottom: 0; }

  .receipt { display: flex; align-items: center; gap: 18px; padding: 10px 13px; border: 1px solid var(--rule-soft); background: var(--surface); color: var(--ink-mid); font-size: var(--fs-ui); flex-wrap: wrap; }
  .margin b { color: var(--green); font-weight: 600; }
  .margin.collision b { color: var(--sev-blocker-fg); }
  .verdict { margin-left: auto; color: var(--ink-soft); font-size: var(--fs-mono-sm); }
  .verdict.bad { color: var(--sev-blocker-fg); }

  .warnings { list-style: none; margin: 14px 0 0; padding: 0; }
  .warnings li { padding: 8px 13px; border: 1px solid var(--rule-soft); border-bottom: 0; background: var(--surface); color: var(--ink-mid); font-size: var(--fs-ui); }
  .warnings li::before { content: '⚠ '; color: var(--sev-marginal-fg, var(--ink-soft)); }
  .warnings li:last-child { border-bottom: 1px solid var(--rule-soft); }

  .params, .source { margin-top: 14px; border: 1px solid var(--rule-soft); background: var(--surface); }
  .heading { display: flex; justify-content: space-between; padding: 11px 13px; border-bottom: 1px solid var(--rule-soft); }
  .heading .mono { color: var(--ink-soft); font-size: var(--fs-mono-sm); }

  table { width: 100%; border-collapse: collapse; }
  td { padding: 8px 13px; border-bottom: 1px solid var(--rule-soft); }
  tr:last-child td { border-bottom: 0; }
  code { color: var(--ink); font-family: var(--font-mono); }
  .value { text-align: right; color: var(--ink); }

  /* The height cap is load-bearing, not cosmetic: a STEP assembly runs to tens
     of thousands of lines where the v1 source text ran to a few hundred, so an
     uncapped <pre> would lay the whole file out and stretch the tab to its
     length. `contain` keeps that layout from escaping the panel. */
  pre { margin: 0; padding: 12px 13px; overflow-x: auto; max-height: 60vh; overflow-y: auto; contain: content; font-family: var(--font-mono); font-size: var(--fs-mono-sm); line-height: 1.55; color: var(--ink); }

  .empty { padding: 22px 24px; border: 1px solid var(--rule-soft); background: var(--surface); }
  .empty p { margin-top: 8px; color: var(--ink-mid); line-height: 1.5; max-width: 70ch; }

  @media (max-width: 760px) {
    header { flex-direction: column; }
    .receipt { align-items: flex-start; flex-direction: column; gap: 7px; }
    .verdict { margin-left: 0; }
  }
</style>
