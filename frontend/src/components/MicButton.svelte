<script>
  // Dictation into a text field. The component owns the microphone session
  // and the interim bubble; the OWNER of the field owns the append — final
  // transcripts leave through `onappend`, so typed text can never be replaced
  // from here. Listening starts only on a click, ends on the click, on
  // silence (the engine's own auto-stop), or on an error worth showing.
  // Firefox has no SpeechRecognition, so it gets an honest notice in the
  // slot instead of a dead button.
  import { onDestroy } from 'svelte'
  import { createDictation, recognitionCtorOf } from '../lib/voice.js'

  let { onappend, context = 'intent' } = $props()

  let listening = $state(false)
  let interim = $state('')
  let error = $state('')

  const dictation = createDictation(recognitionCtorOf(globalThis), {
    onState: (on) => {
      listening = on
    },
    onInterim: (text) => {
      interim = text
    },
    onFinal: (text) => onappend?.(text),
    onError: (message) => {
      error = message
    },
  })

  function toggle() {
    if (listening) {
      dictation.stop()
    } else {
      error = ''
      dictation.start()
    }
  }

  // A left-open microphone surviving the component is the one leak this
  // control could have; stop() is graceful and harmless when idle.
  onDestroy(() => dictation.stop())

  const label = $derived(
    listening ? 'Stop dictating' : 'Dictate — speech is appended to the field',
  )
</script>

{#if !dictation.supported}
  <span class="notice" data-testid="mic-unsupported" data-context={context}>
    Voice input is not available in this browser
  </span>
{:else}
  <span class="voice">
    {#if listening}
      <span class="bubble" data-testid="mic-interim" data-context={context} data-material="popover" role="status">
        {interim || 'Listening…'}
      </span>
    {:else if error}
      <span class="bubble error" data-testid="mic-error" data-context={context} data-material="popover" role="alert">
        {error}
      </span>
    {/if}
    <button
      type="button"
      class="mic"
      class:listening
      data-testid="mic-button"
      data-context={context}
      aria-pressed={listening}
      title={label}
      aria-label={label}
      onclick={toggle}
    >
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
        <rect x="5.5" y="1.5" width="5" height="8" rx="2.5" stroke="currentColor" stroke-width="1.3" />
        <path d="M3 7.5v.5a5 5 0 0 0 10 0v-.5M8 13v2M5.5 15h5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" />
      </svg>
    </button>
  </span>
{/if}

<style>
  .voice {
    position: relative;
    display: inline-flex;
    flex-shrink: 0;
  }

  .mic {
    width: var(--mic-slot);
    height: var(--mic-slot);
    display: flex;
    align-items: center;
    justify-content: center;
    background: transparent;
    border: 1px solid var(--rule-soft);
    border-radius: var(--radius);
    color: var(--ink-faint);
    flex-shrink: 0;
  }
  .mic:hover { border-color: var(--rule); color: var(--ink-mid); }
  /* Live is unmistakable: the one filled-accent treatment, like the run
     button, because an open microphone must never look idle. */
  .mic.listening {
    background: var(--accent);
    border-color: var(--accent);
    color: var(--accent-ink);
  }

  .bubble {
    position: absolute;
    bottom: calc(100% + 8px);
    right: 0;
    width: max-content;
    max-width: min(340px, 72vw);
    padding: 8px 11px;
    background: var(--surface);
    border: 1px solid var(--rule);
    box-shadow: 0 2px 10px var(--shadow-pop);
    color: var(--ink-mid);
    font-size: var(--fs-ui);
    line-height: 1.45;
    z-index: 4;
  }
  .bubble.error { color: var(--sev-blocker-fg); border-color: var(--sev-blocker-rule); }

  .notice {
    align-self: center;
    max-width: 180px;
    color: var(--ink-faint);
    font-size: var(--fs-mono-sm);
    line-height: 1.35;
  }
</style>
