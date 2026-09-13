<script>
  // The three glyphs from design/Review.dc.html, inline so nothing is fetched.
  let { name, size = 13, color = null } = $props()

  const stroke = $derived(
    color || (name === 'brand' ? 'var(--oxblood)' : name === 'check' ? 'var(--green)' : 'var(--ink-soft)'),
  )
</script>

{#if name === 'brand'}
  <svg width={size} height={size} viewBox="0 0 20 20" fill="none" aria-hidden="true" data-testid="icon" data-icon={name}>
    <path d="M3 2.5 L16.5 10 L3 17.5 Z" {stroke} stroke-width="1.6" stroke-linejoin="round" />
    <path d="M0.5 6.5 H3 M0.5 13.5 H3 M16.5 10 H19.5" {stroke} stroke-width="1.6" />
  </svg>
{:else if name === 'document'}
  <svg width={(size * 12) / 14} height={size} viewBox="0 0 12 14" fill="none" aria-hidden="true" data-testid="icon" data-icon={name}>
    <path d="M1 1h6l4 4v8H1z" {stroke} stroke-width="1.2" stroke-linejoin="round" />
    <path d="M7 1v4h4" {stroke} stroke-width="1.2" />
  </svg>
{:else if name === 'check'}
  <svg width={size} height={size} viewBox="0 0 14 14" fill="none" aria-hidden="true" data-testid="icon" data-icon={name}>
    <path d="M2 7.5 L5.5 11 L12 3.5" {stroke} stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />
  </svg>
{/if}

<style>
  svg { display: block; flex-shrink: 0; }
</style>
