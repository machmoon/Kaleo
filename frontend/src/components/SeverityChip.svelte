<script>
  import { severityInfo } from '../lib/severity.js'

  let { severity } = $props()
  const info = $derived(severityInfo(severity))
</script>

<span class="mono chip" data-testid="severity-chip" data-sev={info.key}>{info.label}</span>

<style>
  .chip {
    font-size: var(--fs-chip);
    letter-spacing: .09em;
    text-transform: uppercase;
    padding: 2px 7px;
    white-space: nowrap;
  }
  .chip[data-sev='blocker'] { color: var(--sev-blocker-fg); background: var(--sev-blocker-bg); }
  .chip[data-sev='marginal'] { color: var(--sev-marginal-fg); background: var(--sev-marginal-bg); }
  .chip[data-sev='note'] { color: var(--sev-note-fg); background: var(--sev-note-bg); }
</style>
