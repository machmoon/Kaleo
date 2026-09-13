// The in-app guided pointer: step one of docs/guided-cursor.md §5 —
// architecture (a), pointing only inside our own webview. MudrikNow's design
// survives the port even though none of its code does: the offer is a consent
// gate (pointing starts only on an explicit Start), advancing is
// user-confirmed (a Next button, never click detection — upstream shipped
// with its global mouse hook deliberately disabled), and refusing to point is
// first-class. A step whose target does not resolve renders caption-only and
// says so, because a pointer on the wrong element teaches the wrong lesson
// with total confidence — the one failure a teaching tool must not have.
//
// Shaped like theme.js/skin.js: decisions about values, with the writable
// store as the one seam the component subscribes to (the run.js pattern). DOM
// measurement lives in GuidePointer.svelte; the resolver here takes an
// injected queryFn so vitest runs this in node. Targets are declarative
// {testid, attrs, within} rather than raw selectors or elements so the future
// screen-bounds provider (architecture (b)) replaces resolveTarget, not the
// guide — and so the state can never hold a stale element.

import { get, writable } from 'svelte/store'
import { logEvent } from './log.js'

const IDLE = Object.freeze({ phase: 'idle', steps: [], step: -1, label: '' })

/** { phase: 'idle'|'offered'|'step'|'done'|'dismissed', steps, step, label }.
    done and dismissed render nothing, like idle; they exist so the exit is
    nameable — the log line and any later UI read the phase, not a guess. */
export const guide = writable(IDLE)

/** Attribute values arrive from findings and refs — free text from a model.
    Escaped so a quote in a ref cannot close the attribute selector early and
    match some unrelated element, which is the wrong-pointer bug in one step. */
function escapeAttr(value) {
  return String(value).replace(/["\\]/g, '\\$&')
}

/** One node of a selector: a data-testid plus identity attributes. Identity
    attrs, never an index — the repo's convention is that repeated rows share
    one testid and are told apart by data-ref / data-sev, and an index would
    silently rebind the pointer when the list reorders. */
function nodeSelector(node) {
  if (!node || typeof node.testid !== 'string' || !node.testid) return ''
  let out = `[data-testid="${escapeAttr(node.testid)}"]`
  const attrs = node.attrs || {}
  for (const key of Object.keys(attrs)) {
    out += `[data-${key}="${escapeAttr(attrs[key])}"]`
  }
  return out
}

/** The CSS selector for a step target, optionally scoped under an ancestor
    (`within`, built the same way). '' for a null or malformed target — and a
    `within` that cannot be built empties the whole selector rather than
    silently widening the query to the whole document. */
export function targetSelector(target) {
  const own = nodeSelector(target)
  if (!own) return ''
  if (target.within !== undefined && target.within !== null) {
    const scope = nodeSelector(target.within)
    if (!scope) return ''
    return `${scope} ${own}`
  }
  return own
}

/** The element a step points at right now, or null. Resolved at render time,
    per step, never cached from offer time: the app moves on between the offer
    and the step (a tab switch, a cleared selection), and pointing at where
    something used to be is exactly the failure this feature exists to avoid.
    null is an answer, not an error — the component renders the caption alone
    and says nothing is highlighted. */
export function resolveTarget(target, queryFn) {
  const selector = targetSelector(target)
  if (!selector) return null
  try {
    return queryFn(selector) ?? null
  } catch {
    // A malformed selector or a torn-down document refuses, it never throws
    // out of a render.
    return null
  }
}

/** One step as the machine keeps it. A step must carry a caption — the
    caption is the fallback when the pointer refuses, so a captionless step
    has no honest degraded form and is dropped. A target is kept only when it
    can name an element; anything else becomes the deliberate caption-only
    target, null. */
function normalizeStep(step) {
  const caption = String(step?.caption ?? '').trim()
  const raw = step?.target
  const target =
    raw && typeof raw.testid === 'string' && raw.testid
      ? {
          testid: raw.testid,
          attrs: { ...(raw.attrs || {}) },
          ...(raw.within ? { within: { testid: String(raw.within.testid ?? ''), attrs: { ...(raw.within.attrs || {}) } } } : {}),
        }
      : null
  const tab = typeof step?.tab === 'string' ? step.tab : ''
  return { target, caption, tab }
}

/** Offers a guide: idle → offered. Nothing points yet — the offer is the
    consent gate, and the pointer waits for startGuide(). Returns whether an
    offer actually stands, so a caller can keep its button honest. */
export function offerGuide(steps, label = '') {
  const list = (Array.isArray(steps) ? steps : []).map(normalizeStep).filter((s) => s.caption)
  if (!list.length) return false
  guide.set({ phase: 'offered', steps: list, step: -1, label: String(label ?? '') })
  logEvent('guide.offer', `guide offered: ${list.length} steps`, {
    steps: list.length,
    label: String(label ?? ''),
  })
  return true
}

/** offered → step 0. From any other phase this is a no-op, not a restart: an
    out-of-band start is the protocol violation MudrikNow throws on, and here
    ignoring it is the same guarantee without taking the page down. */
export function startGuide() {
  const state = get(guide)
  if (state.phase !== 'offered') return
  guide.set({ ...state, phase: 'step', step: 0 })
  logEvent('guide.start', 'guide started', { steps: state.steps.length, label: state.label })
}

/** Advances one step; past the last step the guide is done. */
export function nextStep() {
  const state = get(guide)
  if (state.phase !== 'step') return
  const next = state.step + 1
  if (next >= state.steps.length) {
    guide.set({ ...IDLE, phase: 'done' })
    logEvent('guide.done', 'guide finished', { steps: state.steps.length, label: state.label })
    return
  }
  guide.set({ ...state, step: next })
  logEvent('guide.step', `guide step ${next + 1} of ${state.steps.length}`, {
    step: next,
    steps: state.steps.length,
  })
}

/** Ends the guide from the offer or mid-step. The reason goes in the log, not
    the state: nothing downstream branches on why, but a triage read wants it. */
export function dismissGuide(reason = 'dismissed') {
  const state = get(guide)
  if (state.phase !== 'offered' && state.phase !== 'step') return
  guide.set({ ...IDLE, phase: 'dismissed' })
  logEvent('guide.dismiss', `guide dismissed (${reason})`, {
    reason: String(reason ?? ''),
    phase: state.phase,
    step: state.step,
  })
}

/** Back to idle without a log line: for run resets, where the findings the
    guide pointed into are gone and a "dismissed" record would blame the user. */
export function resetGuide() {
  guide.set(IDLE)
}

/** The step currently pointing, or null in every other phase. */
export function activeStep(state) {
  if (!state || state.phase !== 'step') return null
  return state.steps[state.step] ?? null
}

export function isLastStep(state) {
  return Boolean(state) && state.phase === 'step' && state.step === state.steps.length - 1
}

// ---------------------------------------------------------------- builders

/** One-step guide from a /desk/resolve answer. Caption-only when there is
    no target or the target cannot name an element — the pointer refuses
    rather than guessing. Starts immediately: the engineer already asked
    "what's this", which is the consent. */
export function applyDeskResolve(result) {
  const caption = String(result?.caption ?? '').trim()
  if (!caption) return false
  const raw = result?.abstain ? null : result?.target
  const target =
    raw && typeof raw.testid === 'string' && raw.testid
      ? { testid: raw.testid, attrs: { ...(raw.attrs || {}) } }
      : null
  const tab = typeof raw?.tab === 'string' ? raw.tab : ''
  const offered = offerGuide([{ target, caption, tab }], 'Desk')
  if (offered) startGuide()
  return offered
}

/** The two-step walkthrough for one finding: the card's own "Show on board"
    button, then the first named part in the board well. Both selectors lean
    on identity attributes that already exist — the selected card's
    data-selected and the board part's data-ref — so this builder invents no
    DOM contract of its own. A finding that names no parts still gets a second
    step, deliberately caption-only (target null), because ending the guide
    one step early would look like a crash and pointing at nothing in
    particular would be worse. */
export function findingGuideSteps(finding) {
  const title = String(finding?.title ?? '').trim()
  const ref = ((finding?.parts || []).filter(Boolean).map(String)[0] || '').trim()
  const first = {
    target: {
      testid: 'finding-card-show-board',
      within: { testid: 'finding-card', attrs: { selected: 'true' } },
    },
    caption:
      '"Show on board" jumps to the board with this finding\'s parts highlighted. ' +
      'Click it yourself, or just press Next and the tab switches for you.',
  }
  const second = ref
    ? {
        target: { testid: 'board-well-part', attrs: { ref } },
        tab: 'board',
        caption: `This is ${ref} on the placed board${title ? ` — the part "${title}" is about` : ''}. Hover it for its footprint and value.`,
      }
    : {
        target: null,
        tab: 'board',
        caption:
          'This finding names no part reference, so there is nothing to point at on the board — the finding text itself is the whole story.',
      }
  return [first, second]
}
