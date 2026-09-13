<script>
  import Citation from './Citation.svelte'
  import SeverityChip from './SeverityChip.svelte'
  import { formatParts } from '../lib/format.js'
  import { findingGuideSteps, offerGuide } from '../lib/guide.js'
  import { severityInfo } from '../lib/severity.js'

  let {
    finding,
    selected = false,
    schematicEnabled = false,
    boardEnabled = false,
    onselect = null,
    onshowschematic = null,
    onshowboard = null,
    onspeak = null,
  } = $props()

  const info = $derived(severityInfo(finding.severity))
  const parts = $derived((finding.parts || []).filter(Boolean))
  const scope = $derived(formatParts(finding.parts))
  const fix = $derived(String(finding.suggested_fix || '').trim())
  const citation = $derived(String(finding.citation || '').trim())

  // Selecting a finding with no parts would highlight nothing, so the card
  // only becomes a control once there is something to point at.
  const selectable = $derived(Boolean(onselect) && parts.length > 0)

  // The card offers the guide itself rather than threading a callback down
  // through ReviewResults: everything the two steps need is already here.
  // Selecting first keeps step one's selector honest — it scopes to the
  // selected card, and asking for a guide is as deliberate a selection as a
  // click on the title. Guarded so a re-tap cannot toggle the selection off
  // and leave the guide pointing into an unselected card.
  function guideMe() {
    if (!selected && onselect) onselect()
    offerGuide(findingGuideSteps(finding), `Walk through "${finding.title}"?`)
  }
</script>

<article class="card" data-testid="finding-card" data-material="panel" data-sev={info.key} data-selected={selected} data-parts={parts.join(' ')} class:selected>
  <!-- The button sits inside the heading so the card keeps its outline level
       while the chips and title become one selection target. -->
  <h3 class="title">
    {#if selectable}
      <button type="button" class="head" data-testid="finding-card-head" data-interactive="true" aria-pressed={selected} onclick={onselect}>
        <span class="chips">
          <SeverityChip severity={finding.severity} />
          {#if scope}<span class="mono scope" data-testid="finding-card-scope">{scope}</span>{/if}
        </span>
        <span class="text" data-testid="finding-card-title">{finding.title}</span>
      </button>
    {:else}
      <span class="head static" data-testid="finding-card-head" data-interactive="false">
        <span class="chips">
          <SeverityChip severity={finding.severity} />
          {#if scope}<span class="mono scope" data-testid="finding-card-scope">{scope}</span>{/if}
        </span>
        <span class="text" data-testid="finding-card-title">{finding.title}</span>
      </span>
    {/if}
  </h3>

  {#if finding.detail}<p class="detail">{finding.detail}</p>{/if}

  {#if citation || fix || onspeak || (selectable && (schematicEnabled || boardEnabled))}
    <div class="footer" data-testid="finding-card-footer">
      <Citation {citation} />
      {#if onspeak}
        <!-- Speaks exactly what the card shows — severity, parts, title,
             detail, fix, citation — composed in voice.js, never a summary. -->
        <button type="button" class="view" data-testid="finding-card-speak" title="Read this finding aloud" onclick={onspeak}>Read aloud</button>
      {/if}
      {#if selectable && schematicEnabled}
        <button type="button" class="view" data-testid="finding-card-show-schematic" onclick={onshowschematic}>Show on schematic</button>
      {/if}
      {#if selectable && boardEnabled}
        <button type="button" class="view" data-testid="finding-card-show-board" onclick={onshowboard}>Show on board</button>
        <!-- Inert secondary styling like the buttons beside it: the guide
             points and narrates, it never applies anything. -->
        <button type="button" class="view" data-testid="finding-card-guide" onclick={guideMe}>Guide me</button>
      {/if}
      <div class="spacer"></div>
      {#if fix}
        <!-- Secondary style on purpose: nothing in the app applies a fix, so
             this must not read as the primary action. -->
        <button type="button" class="fix" data-testid="finding-card-fix" title="Suggested fix — nothing is applied automatically">{fix}</button>
      {/if}
    </div>
  {/if}
</article>

<style>
  .card {
    background: var(--surface);
    border: 1px solid var(--rule-soft);
    border-left: var(--sev-bar-w) solid var(--sev-note-rule);
    padding: 15px 18px;
  }
  .card[data-sev='blocker'] { border-left-color: var(--sev-blocker-rule); }
  .card[data-sev='marginal'] { border-left-color: var(--sev-marginal-rule); }
  .card.selected { background: var(--well); border-color: var(--rule); }

  .title {
    font-size: var(--fs-card-title);
    font-weight: 600;
    letter-spacing: -.005em;
    margin-bottom: 6px;
  }

  .head {
    display: block;
    width: 100%;
    padding: 0;
    background: transparent;
    border: none;
    text-align: left;
    font: inherit;
    color: inherit;
  }
  .head.static { cursor: default; }

  .chips { display: flex; align-items: center; gap: 11px; margin-bottom: 7px; flex-wrap: wrap; }
  .scope { font-size: var(--fs-mono); color: var(--teal); font-weight: 400; }
  .text { display: block; }

  button.head:hover .text { color: var(--oxblood); }
  button.head[aria-pressed='true'] .text { color: var(--oxblood); }

  .detail {
    font-size: var(--fs-detail);
    color: var(--ink-mid);
    line-height: 1.6;
    margin-bottom: 11px;
    max-width: var(--measure-detail);
  }

  .footer {
    display: flex;
    align-items: center;
    gap: 16px;
    padding-top: 10px;
    border-top: 1px solid var(--rule-soft);
    flex-wrap: wrap;
  }
  .spacer { flex-grow: 1; }

  .fix, .view {
    font-size: 12px;
    padding: 6px 13px;
    background: transparent;
    color: var(--ink-mid);
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    text-align: left;
  }
  .view { white-space: nowrap; }
</style>
