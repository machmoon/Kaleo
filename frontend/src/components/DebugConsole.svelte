<script module>
  // The drawer unmounts when it closes, so the height a reader dragged to has
  // to outlive the instance. Module scope, not localStorage: it is a reading
  // posture for this page, not a setting worth carrying between visits.
  // NaN until the first open, which clampHeight reads as "use the default".
  let lastHeight = Number.NaN
</script>

<script>
  import { MIN_HEIGHT, clampHeight, maxHeight, stepHeight } from '../lib/console-size.js'
  import { downloadText } from '../lib/download.js'
  import { formatCount, joinDot } from '../lib/format.js'
  import {
    LOG_NDJSON_MIME,
    LOG_TEXT_MIME,
    clearLog,
    log,
    logEvent,
    logFilename,
    toNdjson,
    toText,
  } from '../lib/log.js'

  let { onclose = null } = $props()

  /** Rows rendered before the list is clipped. The buffer holds a thousand;
      painting all of them on every flush is what makes a console drawer stutter. */
  const VISIBLE = 200
  /** Distance from the bottom that still counts as "reading the tail". */
  const STICK_PX = 8
  const MAX_DATA_CHARS = 300
  const COPY_FLASH_MS = 1200
  /** Quiet time that ends a run of arrow keys. One entry per burst, not one
      per key: a reader nudging the drawer taller is doing one thing. */
  const KEY_BURST_MS = 400

  let errorsOnly = $state(false)
  let showExt = $state(false)
  let showAll = $state(false)
  let copyLabel = $state('Copy')
  let list = $state(null)
  let viewport = $state(window.innerHeight)
  let height = $state(clampHeight(lastHeight, window.innerHeight))

  // Plain lets: neither is read during render, and making them reactive would
  // schedule an update for something only an effect ever looks at.
  let stick = true
  let copyTimer = 0

  let dragging = false
  let dragFrom = 0
  let grabY = 0
  let grabHeight = 0
  let keyTimer = 0
  let keyFrom = 0

  const ceiling = $derived(maxHeight(viewport))

  const entries = $derived($log.entries)
  const errors = $derived(entries.filter((e) => e.level === 'error').length)
  const warnings = $derived(entries.filter((e) => e.level === 'warn').length)
  const counts = $derived(
    joinDot([
      formatCount(entries.length, 'entry', 'entries'),
      formatCount(errors, 'error'),
      formatCount(warnings, 'warning'),
    ]),
  )

  // The two filters compose. Extension noise is out of the way by default --
  // an injected script warning about its own wallet is not about this board --
  // and "errors only" narrows whatever is left of the buffer after that.
  const visible = $derived(showExt ? entries : entries.filter((e) => !e.ext))
  const extHidden = $derived(entries.length - visible.length)
  const shown = $derived(errorsOnly ? visible.filter((e) => e.level === 'error') : visible)
  const rows = $derived(!showAll && shown.length > VISIBLE ? shown.slice(-VISIBLE) : shown)
  const hidden = $derived(shown.length - rows.length)

  // Both the filter and the row clip are view concerns: what Copy and the two
  // exports write is always the whole buffer. A file whose header counts the
  // buffer but whose body counts the filter is a file that lies about the run.
  const exported = $derived(entries)

  /** UTC, the same clock the exporters stamp, so a row read off the screen and
      the same row in an exported file carry the same time. */
  function clock(ts) {
    return new Date(Number(ts) || 0).toISOString().slice(11, 23)
  }

  function dataText(entry) {
    if (entry.data === null || entry.data === undefined) return ''
    let text
    try {
      text = JSON.stringify(entry.data) ?? ''
    } catch {
      return ''
    }
    if (text === '{}' || text === '[]') return ''
    return text.length > MAX_DATA_CHARS ? `${text.slice(0, MAX_DATA_CHARS)}…` : text
  }

  function flash(label) {
    copyLabel = label
    clearTimeout(copyTimer)
    copyTimer = setTimeout(() => (copyLabel = 'Copy'), COPY_FLASH_MS)
  }

  async function copy() {
    try {
      // Throws rather than resolving when the page is not a secure context,
      // which is exactly the case the transient label has to report.
      await navigator.clipboard.writeText(toText(exported))
      flash('Copied')
    } catch {
      flash('Copy failed')
    }
  }

  function exportText() {
    downloadText(toText(exported), logFilename('txt'), LOG_TEXT_MIME)
  }

  function exportNdjson() {
    downloadText(toNdjson(exported), logFilename('ndjson'), LOG_NDJSON_MIME)
  }

  function clear() {
    showAll = false
    clearLog()
  }

  /** True for a text control outside the drawer -- the Escape is that field's. */
  function typingOutside(node) {
    if (!node || typeof node.tagName !== 'string') return false
    const editable = ['INPUT', 'TEXTAREA', 'SELECT'].includes(node.tagName) || node.isContentEditable
    return editable && !node.closest?.('#debug-console')
  }

  function close() {
    onclose?.()
    // Focus goes back to the control that opened the drawer; losing it to the
    // body would strand a keyboard user at the top of the document.
    document.querySelector('#debug-console-toggle')?.focus()
  }

  function noteResize(from, to) {
    if (from === to) return
    logEvent('ui.console-resize', `console ${from} → ${to} px`, { from, to })
  }

  function startDrag(event) {
    if (event.button > 0) return
    dragging = true
    dragFrom = height
    grabY = event.clientY
    grabHeight = height
    event.currentTarget.setPointerCapture(event.pointerId)
    // The drawer's top edge sits against the body's text; without this a drag
    // that starts on the grip selects whatever it passes over.
    event.preventDefault()
  }

  function onDrag(event) {
    if (!dragging) return
    // Upward is taller: the drawer is anchored to the bottom of the window.
    height = clampHeight(grabHeight + (grabY - event.clientY), viewport)
  }

  function endDrag(event) {
    if (!dragging) return
    dragging = false
    // Capture is released implicitly by the browser on pointerup, and asking
    // for a pointer it no longer holds throws.
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId)
    }
    noteResize(dragFrom, height)
  }

  /** Ends a run of arrow keys, whether it ran out of quiet time or the drawer
      closed underneath it. Either way the burst is one entry. */
  function flushKeys() {
    if (!keyTimer) return
    clearTimeout(keyTimer)
    keyTimer = 0
    noteResize(keyFrom, height)
  }

  function onGripKey(event) {
    const direction = event.key === 'ArrowUp' ? 1 : event.key === 'ArrowDown' ? -1 : 0
    if (!direction) return
    // Kept off the page: the arrows would otherwise scroll the entry list.
    event.preventDefault()
    const to = stepHeight(height, direction, viewport)
    if (to === height) return
    if (!keyTimer) keyFrom = height
    height = to
    clearTimeout(keyTimer)
    keyTimer = setTimeout(flushKeys, KEY_BURST_MS)
  }

  // Measured before the rows are patched in: once a new entry is in the DOM the
  // old scroll position no longer says whether the tail was being read.
  $effect.pre(() => {
    rows
    if (list) stick = list.scrollHeight - list.scrollTop - list.clientHeight <= STICK_PX
  })

  $effect(() => {
    rows
    if (list && stick) list.scrollTop = list.scrollHeight
  })

  $effect(() => {
    const onkey = (event) => {
      if (event.key !== 'Escape') return
      // Escape belongs to whatever field has focus -- clearing a combobox,
      // reverting an edit -- unless that field is one of the drawer's own.
      if (typingOutside(event.target)) return
      close()
    }
    window.addEventListener('keydown', onkey)
    return () => window.removeEventListener('keydown', onkey)
  })

  // Written here rather than at each assignment, so every path that changes the
  // height -- drag, arrows, a shrunken window -- is remembered by one of them.
  $effect(() => {
    lastHeight = height
  })

  // A drawer left at 80% of a tall window would be taller than a short one.
  $effect(() => {
    const onresize = () => {
      viewport = window.innerHeight
      height = clampHeight(height, viewport)
    }
    window.addEventListener('resize', onresize)
    return () => window.removeEventListener('resize', onresize)
  })

  $effect(() => () => {
    clearTimeout(copyTimer)
    flushKeys()
    // Escape can close the drawer mid-drag; pointerup never reaches the
    // unmounted grip, but the height change is already persisted.
    if (dragging) {
      dragging = false
      noteResize(dragFrom, height)
    }
  })
</script>

<section
  id="debug-console"
  class="console"
  aria-label="Debug console"
  style:height="{height}px"
  data-testid="debug-console"
  data-material="chrome"
>
  <!-- A focusable separator is the ARIA window-splitter pattern, and the one
       role aria-valuenow is defined on; Svelte's checker reads every separator
       as decorative. -->
  <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
  <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
  <div
    class="grip"
    role="separator"
    tabindex="0"
    aria-orientation="horizontal"
    aria-label="Resize console"
    aria-valuemin={MIN_HEIGHT}
    aria-valuemax={ceiling}
    aria-valuenow={height}
    onpointerdown={startDrag}
    onpointermove={onDrag}
    onpointerup={endDrag}
    onpointercancel={endDrag}
    onkeydown={onGripKey}
    data-testid="debug-console-resize"
  ></div>

  <div class="toolbar" data-testid="debug-console-toolbar">
    <span class="lbl">Console</span>
    <span class="mono counts" data-testid="debug-console-counts">{counts}</span>
    <!-- Says why the rows are fewer than the count beside them. Only when the
         filter is actually holding something back. -->
    {#if extHidden && !showExt}
      <span class="mono ext" data-testid="debug-console-ext-hidden">· {extHidden} ext hidden</span>
    {/if}
    <!-- Only shown when the buffer actually lost something: a standing
         "dropped 0" would read as a warning about nothing. -->
    {#if $log.dropped}
      <span class="mono dropped" data-testid="debug-console-dropped">dropped {$log.dropped}</span>
    {/if}

    <span class="spacer"></span>

    <button
      type="button"
      class="btn"
      class:on={errorsOnly}
      aria-pressed={errorsOnly}
      onclick={() => (errorsOnly = !errorsOnly)}
      data-testid="debug-console-filter-errors"
    >Errors only</button>
    <button
      type="button"
      class="btn"
      class:on={showExt}
      aria-pressed={showExt}
      onclick={() => (showExt = !showExt)}
      data-testid="debug-console-filter-ext"
    >Show extensions</button>
    <button type="button" class="btn" onclick={copy} data-testid="debug-console-copy">
      {copyLabel}
    </button>
    <button type="button" class="btn" onclick={exportText} data-testid="debug-console-export-text">
      Export .txt
    </button>
    <button
      type="button"
      class="btn"
      onclick={exportNdjson}
      data-testid="debug-console-export-ndjson"
    >Export .ndjson</button>
    <button type="button" class="btn" onclick={clear} data-testid="debug-console-clear">
      Clear
    </button>
    <button type="button" class="btn" onclick={close} data-testid="debug-console-close">
      Close
    </button>
  </div>

  <ul class="rows mono" bind:this={list} data-testid="debug-console-list">
    {#if hidden > 0}
      <li class="more">
        <button
          type="button"
          class="btn"
          onclick={() => (showAll = true)}
          data-testid="debug-console-show-all"
        >Show all {shown.length}</button>
      </li>
    {/if}

    <!-- Keyed by seq, not by index: head eviction renumbers every index. -->
    {#each rows as entry (entry.seq)}
      <li
        class="row"
        data-testid="debug-console-entry"
        data-level={entry.level}
        data-src={entry.src}
        data-seq={entry.seq}
        data-event={entry.event}
      >
        <span class="time">{clock(entry.ts)}</span>
        <span class="tag">{entry.level.toUpperCase()}</span>
        <span class="src">{entry.src}</span>
        {#if entry.run}<span class="run">{entry.run}</span>{/if}
        <span class="msg">{entry.msg}</span>
        {#if dataText(entry)}<span class="data">{dataText(entry)}</span>{/if}
      </li>
    {/each}

    {#if !shown.length}
      <li class="empty" data-testid="debug-console-empty">
        {#if errorsOnly}
          No errors captured. Clear the filter to see every entry.
        {:else if extHidden}
          Only browser-extension output was captured. Show extensions to read it.
        {:else}
          Nothing captured yet. Console output, page errors, and run events appear here.
        {/if}
      </li>
    {/if}
  </ul>
</section>

<style>
  /* A flex sibling of the body, not an overlay: the app has no z-index scale,
     so the drawer pushes the layout rather than covering it. */
  .console {
    flex-shrink: 0;
    display: flex;
    flex-direction: column;
    min-height: 0;
    background: var(--well);
    border-top: 1px solid var(--rule);
  }

  /* Pulled up over the drawer's top border, so the hairline someone aims at is
     inside the target rather than one pixel above it. */
  .grip {
    flex-shrink: 0;
    height: 9px;
    margin-top: -1px;
    cursor: row-resize;
    touch-action: none;
  }
  /* Darkens the border it covers rather than painting a bar of its own: the
     drawer's edge is already the line, and it only has to say it can move. */
  .grip:hover, .grip:focus-visible { box-shadow: inset 0 1px 0 var(--ink-soft); }
  .grip:focus-visible { outline-offset: -2px; }

  .toolbar {
    height: 30px;
    flex-shrink: 0;
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 0 8px 0 14px;
    border-bottom: 1px solid var(--rule-soft);
  }
  .counts { font-size: var(--fs-mono-sm); color: var(--ink-soft); }
  .dropped { font-size: var(--fs-mono-sm); color: var(--sev-marginal-fg); }
  /* Hidden noise is not a warning, so it stays quieter than dropped. */
  .ext { font-size: var(--fs-mono-sm); color: var(--ink-faint); }
  .spacer { flex-grow: 1; }

  /* The secondary button from ReviewResults, sized down to fit a 30 px bar. */
  .btn {
    font-size: 11px;
    padding: 3px 9px;
    background: transparent;
    color: var(--ink-mid);
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    white-space: nowrap;
  }
  .btn:focus-visible { outline-offset: -2px; }
  .btn.on { color: var(--ink); background: var(--surface); border-color: var(--ink-soft); }

  .rows {
    flex-grow: 1;
    overflow-y: auto;
    margin: 0;
    padding: 0;
    list-style: none;
    font-size: var(--fs-mono-sm);
  }

  .row {
    display: flex;
    align-items: baseline;
    gap: 9px;
    padding: 3px 14px;
    line-height: 1.5;
  }
  .row + .row { border-top: 1px solid var(--rule-soft); }

  .time, .tag, .src, .run { flex-shrink: 0; color: var(--ink-soft); }
  .tag { width: 5ch; letter-spacing: .04em; }
  .msg { color: var(--ink); overflow-wrap: anywhere; }
  .data { color: var(--ink-soft); overflow-wrap: anywhere; }

  .row[data-level='error'] { background: var(--sev-blocker-bg); }
  .row[data-level='error'] .tag, .row[data-level='error'] .msg { color: var(--sev-blocker-fg); }
  .row[data-level='warn'] { background: var(--sev-marginal-bg); }
  .row[data-level='warn'] .tag, .row[data-level='warn'] .msg { color: var(--sev-marginal-fg); }
  .row[data-level='info'] .tag { color: var(--ink-mid); }
  .row[data-level='debug'] .tag, .row[data-level='debug'] .msg { color: var(--ink-faint); }

  .more { padding: 6px 14px; border-bottom: 1px solid var(--rule-soft); }

  .empty {
    font-family: var(--font-sans);
    font-size: var(--fs-ui);
    color: var(--ink-faint);
    line-height: 1.65;
    padding: 14px;
  }
</style>
