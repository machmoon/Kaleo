<script>
  import Icon from './Icon.svelte'
  import { formatCount, formatDuration, joinDot } from '../lib/format.js'
  import {
    readStored as readSkin,
    resolveSkin,
    skinAttribute,
    toggleSkin,
    writeStored as writeSkin,
  } from '../lib/skin.js'
  import { prefersDark, readStored, resolveTheme, toggleTheme, writeStored } from '../lib/theme.js'

  let { intent = '', result = null } = $props()

  // Every number here comes off the response; nothing is filled in when there
  // is no run to describe.
  const meta = $derived(
    result
      ? joinDot([
          `read ${formatCount(result.datasheets.length, 'datasheet')}`,
          formatDuration(result.duration_s),
        ])
      : '',
  )

  // The stored choice, not the theme: no choice yet has to stay no choice so
  // the stylesheet keeps following the OS until somebody says otherwise.
  let choice = $state(readStored(globalThis.localStorage))
  const theme = $derived(resolveTheme(choice, prefersDark(globalThis)))

  function flipTheme() {
    choice = toggleTheme(theme)
    writeStored(globalThis.localStorage, choice)
    document.documentElement.dataset.theme = choice
  }

  // The material, orthogonal to the light above it: paper is the drafting
  // table, glass is the overlay surface. Stored the same way, written the
  // same way, and read back from the same one attribute.
  let skinChoice = $state(readSkin(globalThis.localStorage))
  const skin = $derived(resolveSkin(skinChoice))

  function flipSkin() {
    skinChoice = toggleSkin(skin)
    writeSkin(globalThis.localStorage, skinChoice)
    const attr = skinAttribute(skinChoice)
    // The default skin is the absence of the attribute, not a value that
    // spells it -- glass.css keys on [data-skin='glass'] and nothing else.
    if (attr) document.documentElement.dataset.skin = attr
    else delete document.documentElement.dataset.skin
  }
</script>

<header class="bar" data-testid="title-bar" data-material="chrome">
  <div class="brand" data-testid="title-bar-brand">
    <Icon name="brand" size={17} />
    <span class="wordmark">Hardy</span>
  </div>
  {#if intent}<span class="intent" data-testid="title-bar-intent">{intent}</span>{/if}
  <div class="spacer"></div>
  {#if meta}<span class="mono meta" data-testid="title-bar-meta">{meta}</span>{/if}
  <a class="lbl placement" href="/?mode=placement" data-testid="title-bar-placement">Verifier lab</a>
  <button
    type="button"
    class="lbl chip"
    aria-pressed={skin === 'glass'}
    aria-label="Glass skin"
    onclick={flipSkin}
    data-testid="title-bar-skin"
    data-skin-choice={skin}
  >Glass</button>
  <button
    type="button"
    class="lbl chip"
    aria-pressed={theme === 'dark'}
    aria-label="Night mode"
    onclick={flipTheme}
    data-testid="title-bar-theme"
    data-theme-choice={theme}
  >Night</button>
</header>

<style>
  .bar {
    height: var(--titlebar-h);
    display: flex;
    align-items: center;
    border-bottom: 1px solid var(--rule);
    background: var(--surface);
    flex-shrink: 0;
  }

  .brand {
    display: flex;
    align-items: center;
    gap: 9px;
    padding: 0 16px;
    border-right: 1px solid var(--rule-soft);
    height: 100%;
  }
  .wordmark { font-weight: 600; font-size: 14px; letter-spacing: -.01em; }

  .intent {
    font-size: var(--fs-ui);
    color: var(--ink-mid);
    padding: 0 12px;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .spacer { flex-grow: 1; }
  .meta { font-size: var(--fs-mono-sm); color: var(--ink-soft); padding: 0 16px; }
  .placement { display: flex; align-items: center; height: 100%; padding: 0 16px; border-left: 1px solid var(--rule-soft); color: var(--ink-soft); text-decoration: none; }
  .placement:hover { color: var(--ink); }

  /* The brand rule, hung off the other edge of the bar. Two of them now:
     the material, then the light it is read under. */
  .chip {
    display: flex;
    align-items: center;
    height: 100%;
    padding: 0 16px;
    border: none;
    border-left: 1px solid var(--rule-soft);
    background: transparent;
    color: var(--ink-faint);
  }
  .chip:hover { color: var(--ink); }
  /* Lit like a current tab: pressed means that reading is the one showing. */
  .chip[aria-pressed='true'] { color: var(--ink); }
  /* The global ring sits 1 px outside the element, which the bar clips. */
  .chip:focus-visible { outline-offset: -2px; }

  @media (max-width: 720px) {
    .meta { display: none; }
    .chip { padding: 0 11px; }
  }
</style>
