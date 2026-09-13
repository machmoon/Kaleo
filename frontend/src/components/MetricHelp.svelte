<script>
  let { label, explanation, align = 'centre' } = $props()
</script>

<span class="help" class:left={align === 'left'} class:right={align === 'right'}>
  <button
    type="button"
    aria-label={`${label}. ${explanation}`}
  ><span aria-hidden="true">?</span></button>
  <span class="tooltip" role="tooltip" aria-hidden="true" data-material="popover">
    <strong>{label}</strong>
    <span>{explanation}</span>
  </span>
</span>

<style>
  .help {
    position: relative;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
    vertical-align: middle;
  }

  button {
    position: absolute;
    left: 50%;
    top: 50%;
    width: 44px;
    height: 44px;
    display: inline-grid;
    place-items: center;
    padding: 0;
    border: 0;
    background: transparent;
    color: var(--ink-mid);
    transform: translate(-50%, -50%);
  }

  button span {
    width: 22px;
    height: 22px;
    display: grid;
    place-items: center;
    border: 1px solid var(--rule);
    border-radius: 50%;
    background: var(--surface);
    font-family: var(--font-mono);
    font-size: 12px;
    font-weight: 700;
    line-height: 1;
  }

  button:hover,
  button:focus-visible {
    color: var(--navy);
  }

  button:hover span,
  button:focus-visible span { border-color: var(--navy); }

  .tooltip {
    position: absolute;
    z-index: 20;
    left: 50%;
    bottom: calc(100% + 8px);
    width: min(300px, calc(100vw - 32px));
    padding: 12px 13px;
    border: 1px solid var(--rule);
    background: var(--surface);
    box-shadow: 0 10px 28px var(--shadow-pop);
    color: var(--ink-mid);
    font-family: var(--font-sans);
    font-size: var(--fs-ui);
    font-weight: 400;
    line-height: 1.5;
    opacity: 0;
    pointer-events: none;
    transform: translate(-50%, 5px);
    transition: opacity 120ms ease, transform 120ms ease;
  }

  .tooltip::after {
    content: '';
    position: absolute;
    top: 100%;
    left: 50%;
    width: 9px;
    height: 9px;
    border-right: 1px solid var(--rule);
    border-bottom: 1px solid var(--rule);
    background: var(--surface);
    transform: translate(-50%, -5px) rotate(45deg);
  }

  .tooltip strong,
  .tooltip span {
    display: block;
  }

  .tooltip strong {
    margin-bottom: 4px;
    color: var(--ink);
    font-weight: 650;
  }

  .help:hover .tooltip,
  .help:focus-within .tooltip {
    opacity: 1;
    transform: translate(-50%, 0);
  }

  .help.left .tooltip {
    left: -8px;
    transform: translateY(5px);
  }

  .help.left .tooltip::after {
    left: 18px;
    transform: translateY(-5px) rotate(45deg);
  }

  .help.left:hover .tooltip,
  .help.left:focus-within .tooltip {
    transform: translateY(0);
  }

  .help.right .tooltip {
    right: -8px;
    left: auto;
    transform: translateY(5px);
  }

  .help.right .tooltip::after {
    right: 14px;
    left: auto;
    transform: translateY(-5px) rotate(45deg);
  }

  .help.right:hover .tooltip,
  .help.right:focus-within .tooltip {
    transform: translateY(0);
  }

  @media (prefers-reduced-motion: reduce) {
    .tooltip { transition: none; }
  }
</style>
