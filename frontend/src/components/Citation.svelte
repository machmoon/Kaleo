<script>
  import Icon from './Icon.svelte'

  // The reviewer is told to omit a citation rather than invent one, so an
  // empty string must render nothing at all.
  let { citation = '' } = $props()
  const text = $derived(String(citation || '').trim())
</script>

{#if text}
  <span class="cite" data-testid="citation"><Icon name="document" size={13} />{text}</span>
{/if}
