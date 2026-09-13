<script>
  import { downloadText } from '../lib/download.js'
  import { formatCount } from '../lib/format.js'
  import { logEvent } from '../lib/log.js'
  import {
    BOM_FILENAME,
    BOM_MIME,
    bomCsv,
    canOpenDatasheet,
    datasheetLabel,
    model3dName,
    mpnLabel,
  } from '../lib/sourcing.js'

  // `sourcing` is readSourcing()'s output or null; `stage` is the run store's
  // sourcing stage row (so a failure can say why); `requested` says whether
  // the run asked for sourcing at all. Null renders an explicit state, never
  // a blank pane — the contract's degradation is honest and so is this.
  let { sourcing = null, stage = null, requested = false } = $props()

  const failed = $derived(!sourcing && stage?.state === 'failed')
  const total = $derived(sourcing ? sourcing.parts.length : 0)
  const models = $derived(sourcing ? sourcing.parts.filter((row) => row.model3d).length : 0)

  function save() {
    if (!sourcing) return
    downloadText(bomCsv(sourcing), BOM_FILENAME, BOM_MIME)
  }

  function opened(row) {
    logEvent('ui.datasheet-open', `opened the verified datasheet for ${row.ref}`, {
      ref: row.ref,
      url: row.datasheetUrl,
    })
  }
</script>

<section class="sourcing-tab" data-testid="sourcing-tab">
  {#if sourcing}
    <header>
      <div>
        <span class="lbl">bill of materials · proposed, not verified</span>
        <h1>Sourcing</h1>
        <p>
          One row per placed part. Manufacturer and MPN are the model's
          proposals — no distributor was asked, so nothing here confirms a part
          is real or in stock. A datasheet reads <em>verified</em> only when the
          engine fetched the URL and saw a PDF. 3D models come from KiCad's own
          library, matched by land pattern.
        </p>
      </div>
      <div class="actions">
        <button type="button" class="primary" onclick={save} data-testid="sourcing-download">Download {BOM_FILENAME}</button>
      </div>
    </header>

    <div class="receipt" data-material="panel" data-testid="sourcing-counts">
      <span class="count mono" data-testid="sourcing-count" data-which="proposed">
        <b>{sourcing.proposed}</b> of {total} MPNs proposed
      </span>
      <span class="count mono" data-testid="sourcing-count" data-which="verified">
        <b>{sourcing.verified}</b> {sourcing.verified === 1 ? 'datasheet' : 'datasheets'} verified
      </span>
      <span class="count mono" data-testid="sourcing-count" data-which="models">
        <b>{models}</b> {models === 1 ? '3D model' : '3D models'} attached
      </span>
      {#if sourcing.unresolved > 0}
        <span class="verdict" data-testid="sourcing-verdict">{formatCount(sourcing.unresolved, 'part')} still unresolved — the BOM carries the row, blank</span>
      {:else}
        <span class="verdict" data-testid="sourcing-verdict">every part has a proposal — each one still needs a human check</span>
      {/if}
    </div>

    {#if sourcing.warnings.length}
      <ul class="warnings" data-testid="sourcing-warnings">
        {#each sourcing.warnings as warning, index (index)}
          <li data-testid="sourcing-warning">{warning}</li>
        {/each}
      </ul>
    {/if}

    <section class="bom" data-material="panel">
      <div class="heading">
        <span class="lbl">{BOM_FILENAME}</span>
        <span class="mono">{formatCount(total, 'row')}</span>
      </div>
      <div class="scroll">
        <table data-testid="sourcing-bom">
          <thead>
            <tr>
              <th>ref</th>
              <th>value</th>
              <th>package</th>
              <th>manufacturer · mpn</th>
              <th>datasheet</th>
              <th>3d model</th>
            </tr>
          </thead>
          <tbody>
            {#each sourcing.parts as row (row.ref)}
              <tr data-testid="sourcing-row" data-ref={row.ref} data-kind={row.kind}>
                <td class="mono ref">{row.ref}</td>
                <td class="mono">{row.value}</td>
                <td class="mono soft">{row.package}</td>
                <td>
                  {#if row.mpn}
                    <span class="mpn">
                      {#if row.manufacturer}<span class="maker">{row.manufacturer}</span>{/if}
                      <code data-testid="sourcing-mpn" data-status={row.mpnStatus}>{row.mpn}</code>
                    </span>
                    <span class="tag" data-testid="sourcing-mpn-status" data-status={row.mpnStatus}>{mpnLabel(row)}</span>
                  {:else}
                    <span class="soft mono" data-testid="sourcing-mpn" data-status="none">—</span>
                  {/if}
                  {#if row.note}
                    <div class="note" data-testid="sourcing-note">{row.note}</div>
                  {/if}
                </td>
                <td>
                  {#if canOpenDatasheet(row)}
                    <!-- The only status that becomes a link. rel=noopener because
                         the target is a URL the model wrote and a probe merely
                         confirmed serves a PDF. -->
                    <a
                      class="tag link"
                      href={row.datasheetUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      onclick={() => opened(row)}
                      data-testid="sourcing-datasheet"
                      data-status={row.datasheetStatus}
                    >{datasheetLabel(row.datasheetStatus)} ↗</a>
                  {:else}
                    <span
                      class="tag"
                      title={row.datasheetUrl || ''}
                      data-testid="sourcing-datasheet"
                      data-status={row.datasheetStatus}
                    >{datasheetLabel(row.datasheetStatus)}</span>
                  {/if}
                </td>
                <td>
                  {#if row.model3d}
                    <span class="tag model" title={row.model3d} data-testid="sourcing-model" data-matched="true">{model3dName(row.model3d)}</span>
                  {:else}
                    <span class="soft" data-testid="sourcing-model" data-matched="false">{row.model3dNote || 'no library model'}</span>
                  {/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </section>
  {:else}
    <!-- The honest empty states: failed is not skipped, skipped is not failed. -->
    <div class="empty" data-material="panel" data-testid="sourcing-empty" data-state={failed ? 'failed' : 'absent'}>
      {#if failed}
        <span class="lbl">sourcing failed</span>
        <p>
          The sourcing stage gave up{stage.error ? `: ${stage.error}` : ''}.
          The board itself was still generated and delivered — a sourcing
          failure never fails the run.
        </p>
      {:else if requested}
        <span class="lbl">no bill of materials arrived</span>
        <p>Sourcing was requested, but this run's response carried no parts.</p>
      {:else}
        <span class="lbl">parts were not sourced</span>
        <p>This run did not ask for sourcing. It is opt-in per run: one more model call plus a datasheet probe per part.</p>
      {/if}
    </div>
  {/if}
</section>

<style>
  .sourcing-tab { max-width: 1180px; margin: 0 auto; }
  header { display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; margin-bottom: 14px; }
  h1 { margin-top: 5px; font-size: var(--fs-h1); font-weight: 560; }
  header p { margin-top: 7px; max-width: 70ch; color: var(--ink-mid); line-height: 1.5; }
  header em { font-style: normal; color: var(--green); }
  .actions { display: flex; gap: 8px; }
  .actions button { min-height: 38px; padding: 0 12px; border: 1px solid var(--rule); background: var(--surface); color: var(--ink-mid); white-space: nowrap; }
  .actions button.primary {
    background: var(--accent);
    color: var(--accent-ink);
    border: none;
    border-radius: var(--radius);
    font-weight: 500;
  }

  .receipt { display: flex; align-items: center; gap: 18px; padding: 10px 13px; border: 1px solid var(--rule-soft); background: var(--surface); color: var(--ink-mid); font-size: var(--fs-ui); flex-wrap: wrap; }
  .count b { color: var(--ink); font-weight: 600; }
  .verdict { margin-left: auto; color: var(--ink-soft); font-size: var(--fs-mono-sm); }

  .warnings { list-style: none; margin: 14px 0 0; padding: 0; }
  .warnings li { padding: 8px 13px; border: 1px solid var(--rule-soft); border-bottom: 0; background: var(--surface); color: var(--ink-mid); font-size: var(--fs-ui); }
  .warnings li::before { content: '⚠ '; color: var(--sev-marginal-fg, var(--ink-soft)); }
  .warnings li:last-child { border-bottom: 1px solid var(--rule-soft); }

  .bom { margin-top: 14px; border: 1px solid var(--rule-soft); background: var(--surface); }
  .heading { display: flex; justify-content: space-between; padding: 11px 13px; border-bottom: 1px solid var(--rule-soft); }
  .heading .mono { color: var(--ink-soft); font-size: var(--fs-mono-sm); }
  /* The table, not the page, scrolls sideways: six columns of part numbers
     will not fit a narrow pane, and the app must never scroll horizontally. */
  .scroll { overflow-x: auto; }

  table { width: 100%; border-collapse: collapse; font-size: var(--fs-ui); }
  th { padding: 7px 13px; text-align: left; font-weight: 500; color: var(--ink-soft); font-size: var(--fs-mono-sm); letter-spacing: .04em; text-transform: uppercase; border-bottom: 1px solid var(--rule-soft); white-space: nowrap; }
  td { padding: 8px 13px; border-bottom: 1px solid var(--rule-soft); vertical-align: top; color: var(--ink); }
  tr:last-child td { border-bottom: 0; }
  .ref { font-weight: 600; }
  .soft { color: var(--ink-soft); }
  .mpn { display: inline-flex; flex-direction: column; gap: 2px; margin-right: 8px; }
  .maker { color: var(--ink-mid); font-size: var(--fs-mono-sm); }
  code { color: var(--ink); font-family: var(--font-mono); }
  .note { margin-top: 5px; color: var(--ink-mid); font-size: var(--fs-mono-sm); line-height: 1.45; max-width: 44ch; }

  /* The status badges: verified reads green because it is the one measured
     claim on the page; everything else is ink, and a proposal is deliberately
     not coloured like a pass. */
  .tag { display: inline-block; padding: 1px 7px; border: 1px solid var(--rule-soft); color: var(--ink-mid); font-family: var(--font-mono); font-size: var(--fs-mono-sm); white-space: nowrap; }
  .tag[data-status='verified'] { border-color: var(--green); color: var(--green); }
  .tag[data-status='unreachable'], .tag[data-status='not_pdf'] { color: var(--sev-marginal-fg); border-color: var(--sev-marginal-fg); }
  .tag[data-status='none'] { color: var(--ink-soft); }
  .tag.link { text-decoration: none; }
  .tag.link:hover { background: var(--well); }
  .tag.model { color: var(--navy); border-color: var(--navy); }

  .empty { padding: 22px 24px; border: 1px solid var(--rule-soft); background: var(--surface); }
  .empty p { margin-top: 8px; color: var(--ink-mid); line-height: 1.5; max-width: 70ch; }

  @media (max-width: 760px) {
    header { flex-direction: column; }
    .receipt { align-items: flex-start; flex-direction: column; gap: 7px; }
    .verdict { margin-left: 0; }
  }
</style>
