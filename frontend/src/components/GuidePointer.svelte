<script>
  // The rendering half of the guided pointer (docs/guided-cursor.md §5):
  // guide.js decides which step is showing, this component measures and
  // draws. Fixed-position overlay in our own DOM — no OS window, no capture,
  // no accessibility tree — and it never clicks anything: the pointer shows,
  // the human clicks, the human presses Next. Bounds arrive from
  // getBoundingClientRect via guide.js's resolver, re-taken on scroll and
  // resize, because a halo measured once silently detaches from its target
  // the moment the panel scrolls.
  import { tick } from 'svelte'
  import {
    activeStep,
    dismissGuide,
    guide,
    isLastStep,
    nextStep,
    resolveTarget,
    startGuide,
  } from '../lib/guide.js'

  // Tab switching belongs to App (the hash is its contract); a step that
  // carries a tab only reports the wish upward.
  let { ongoto = null } = $props()

  // The measured target rect in viewport pixels, or null — and null is shown
  // as null: a step whose target does not resolve renders caption-only with
  // a line saying so, never a halo around a guess.
  let rect = $state(null)

  const state = $derived($guide)
  const step = $derived(activeStep(state))
  const showing = $derived(state.phase === 'offered' || state.phase === 'step')
  const missing = $derived(state.phase === 'step' && step !== null && step.target !== null && rect === null)

  const PAD = 6
  const ring = $derived(
    rect
      ? {
          left: rect.left - PAD,
          top: rect.top - PAD,
          width: rect.width + PAD * 2,
          height: rect.height + PAD * 2,
        }
      : null,
  )

  function measure() {
    const active = activeStep($guide)
    if (!active || !active.target) {
      rect = null
      return
    }
    const el = resolveTarget(active.target, (sel) => document.querySelector(sel))
    if (!el) {
      rect = null
      return
    }
    const r = el.getBoundingClientRect()
    // A zero-size rect is an element that exists but is not laid out (a
    // hidden pane); haloing an invisible point is still the wrong pointer.
    rect = r.width || r.height ? { left: r.left, top: r.top, width: r.width, height: r.height } : null
  }

  // Entering a step: switch the tab it asked for, let the pane render, then
  // scroll the target into view and measure. The delayed second measure
  // covers surfaces that size themselves after mount — the board well reads
  // its width through a clientWidth binding, which lands a frame later.
  $effect(() => {
    const phase = state.phase
    const index = state.step
    if (phase !== 'step') {
      rect = null
      return
    }
    const active = state.steps[index]
    if (active && active.tab && ongoto) ongoto(active.tab)
    let stale = false
    ;(async () => {
      await tick()
      if (stale) return
      const el = active && active.target ? resolveTarget(active.target, (sel) => document.querySelector(sel)) : null
      if (el && typeof el.scrollIntoView === 'function') {
        el.scrollIntoView({ block: 'nearest', inline: 'nearest' })
      }
      measure()
    })()
    const settle = setTimeout(measure, 250)
    return () => {
      stale = true
      clearTimeout(settle)
    }
  })

  // Live listeners only while something shows; torn down on phase change and
  // on destroy, so a dismissed guide leaves nothing running.
  $effect(() => {
    if (!showing) return
    const remeasure = () => measure()
    // Capture phase: the panes scroll themselves (`overflow: auto` wells),
    // and their scroll events never bubble to window.
    window.addEventListener('scroll', remeasure, true)
    window.addEventListener('resize', remeasure)
    const onkey = (event) => {
      if (event.key === 'Escape') dismissGuide('escape')
    }
    window.addEventListener('keydown', onkey)
    return () => {
      window.removeEventListener('scroll', remeasure, true)
      window.removeEventListener('resize', remeasure)
      window.removeEventListener('keydown', onkey)
    }
  })

  const CARD_W = 320
  const CARD_MARGIN = 12

  /** Where the caption card sits: pinned near its ring, clamped into the
      viewport, flipped above when there is no room below; centered at the
      bottom when there is nothing measured to pin to. Read at render time —
      resize re-measures, which recomputes this. */
  function cardStyle(r) {
    if (!r) return `left: 50%; bottom: 46px; transform: translateX(-50%); width: ${CARD_W}px;`
    const vw = window.innerWidth
    const vh = window.innerHeight
    const left = Math.max(CARD_MARGIN, Math.min(r.left, vw - CARD_W - CARD_MARGIN))
    const below = r.top + r.height + 14
    if (below + 180 > vh) {
      return `left: ${left}px; top: ${Math.max(CARD_MARGIN, r.top - 14)}px; transform: translateY(-100%); width: ${CARD_W}px;`
    }
    return `left: ${left}px; top: ${below}px; width: ${CARD_W}px;`
  }
</script>

{#if showing}
  <div class="overlay" data-testid="guide-overlay" aria-live="polite">
    {#if state.phase === 'step' && ring}
      <div
        class="ring"
        data-testid="guide-pointer"
        style="left: {ring.left}px; top: {ring.top}px; width: {ring.width}px; height: {ring.height}px;"
      ></div>
      <div class="arrow" style="left: {ring.left + ring.width / 2}px; top: {ring.top - 20}px;"></div>
    {/if}

    <div class="card" data-testid="guide-caption" data-material="popover" style={cardStyle(ring)}>
      {#if state.phase === 'offered'}
        <div class="lbl count" data-testid="guide-step-count">Walkthrough · {state.steps.length} steps</div>
        <p class="text">{state.label || 'Want a walkthrough of this finding?'}</p>
        <div class="row">
          <button type="button" class="primary" data-testid="guide-start" onclick={startGuide}>Start guide</button>
          <button type="button" class="quiet" data-testid="guide-dismiss" onclick={() => dismissGuide('declined')}>
            No thanks
          </button>
        </div>
      {:else if step}
        <div class="lbl count" data-testid="guide-step-count">Step {state.step + 1} of {state.steps.length}</div>
        <p class="text">{step.caption}</p>
        {#if missing}
          <!-- The refusal, said out loud: the honest fallback when the target
               is not on screen, per the design note — a wrong pointer is
               worse than none. -->
          <p class="missing" data-testid="guide-missing">
            The control this step describes is not on screen right now, so nothing is highlighted.
          </p>
        {/if}
        <div class="row">
          {#if isLastStep(state)}
            <button type="button" class="primary" data-testid="guide-done" onclick={nextStep}>Done</button>
          {:else}
            <button type="button" class="primary" data-testid="guide-next" onclick={nextStep}>Next</button>
          {/if}
          <button type="button" class="quiet" data-testid="guide-dismiss" onclick={() => dismissGuide('dismissed')}>
            Dismiss
          </button>
        </div>
      {/if}
    </div>
  </div>
{/if}

<style>
  /* The overlay itself swallows no clicks: the page under the guide stays
     fully usable — the whole point is that the user does the clicking. Only
     the caption card takes the pointer back. */
  .overlay {
    position: fixed;
    inset: 0;
    pointer-events: none;
    z-index: 80;
  }

  .ring {
    position: fixed;
    border: 2px solid var(--guide-ring);
    border-radius: 4px;
    box-shadow: 0 0 0 4px var(--guide-glow);
    animation: guide-pulse 1.6s ease-in-out infinite;
  }

  .arrow {
    position: fixed;
    width: 0;
    height: 0;
    margin-left: -9px;
    border: 9px solid transparent;
    border-top-color: var(--guide-ring);
    animation: guide-bob 1.1s ease-in-out infinite;
  }

  @keyframes guide-pulse {
    0%, 100% { box-shadow: 0 0 0 4px var(--guide-glow); }
    50% { box-shadow: 0 0 0 9px var(--guide-glow); }
  }

  @keyframes guide-bob {
    0%, 100% { transform: translateY(0); }
    50% { transform: translateY(-6px); }
  }

  /* Still ringed and pointed at, just not moving. */
  @media (prefers-reduced-motion: reduce) {
    .ring, .arrow { animation: none; }
  }

  .card {
    position: fixed;
    pointer-events: auto;
    background: var(--surface);
    border: 1px solid var(--rule);
    border-left: var(--sev-bar-w) solid var(--guide-ring);
    padding: 12px 15px 13px;
    box-shadow: 0 2px 8px var(--shadow-pop);
  }

  .count { color: var(--ink-soft); margin-bottom: 6px; }

  .text {
    font-size: var(--fs-detail);
    color: var(--ink);
    line-height: 1.55;
    margin-bottom: 10px;
  }

  .missing {
    font-size: var(--fs-ui);
    color: var(--ink-soft);
    font-style: italic;
    line-height: 1.5;
    margin-bottom: 10px;
  }

  .row { display: flex; align-items: center; gap: 10px; }

  .primary {
    font-size: 12px;
    padding: 6px 14px;
    background: var(--accent);
    color: var(--accent-ink);
    border: 1px solid var(--accent);
    border-radius: var(--radius);
  }

  .quiet {
    font-size: 12px;
    padding: 6px 13px;
    background: transparent;
    color: var(--ink-mid);
    border: 1px solid var(--rule);
    border-radius: var(--radius);
  }
</style>
