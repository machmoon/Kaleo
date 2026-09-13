import { get } from 'svelte/store'
import { beforeEach, describe, expect, it } from 'vitest'
import {
  activeStep,
  applyDeskResolve,
  dismissGuide,
  findingGuideSteps,
  guide,
  isLastStep,
  nextStep,
  offerGuide,
  resetGuide,
  resolveTarget,
  startGuide,
  targetSelector,
} from './guide.js'

// The store is module state, like log.js's buffer; every test starts idle.
beforeEach(() => resetGuide())

const twoSteps = [
  { target: { testid: 'finding-card-show-board' }, caption: 'Click show on board.' },
  { target: { testid: 'board-well-part', attrs: { ref: 'C1' } }, tab: 'board', caption: 'This is C1.' },
]

describe('targetSelector', () => {
  it('builds a bare data-testid selector', () => {
    expect(targetSelector({ testid: 'guide-next' })).toBe('[data-testid="guide-next"]')
  })

  it('appends identity attributes, never an index', () => {
    expect(targetSelector({ testid: 'board-well-part', attrs: { ref: 'C1' } })).toBe(
      '[data-testid="board-well-part"][data-ref="C1"]',
    )
    expect(targetSelector({ testid: 'finding-card', attrs: { sev: 'blocker', selected: 'true' } })).toBe(
      '[data-testid="finding-card"][data-sev="blocker"][data-selected="true"]',
    )
  })

  it('scopes under a within ancestor built the same way', () => {
    expect(
      targetSelector({
        testid: 'finding-card-show-board',
        within: { testid: 'finding-card', attrs: { selected: 'true' } },
      }),
    ).toBe('[data-testid="finding-card"][data-selected="true"] [data-testid="finding-card-show-board"]')
  })

  it('escapes quotes and backslashes so a hostile ref cannot break out of the attribute', () => {
    expect(targetSelector({ testid: 'board-well-part', attrs: { ref: 'C1"] *' } })).toBe(
      '[data-testid="board-well-part"][data-ref="C1\\"] *"]',
    )
    expect(targetSelector({ testid: 'x', attrs: { ref: 'a\\b' } })).toBe(
      '[data-testid="x"][data-ref="a\\\\b"]',
    )
  })

  it('returns nothing for a null or malformed target', () => {
    expect(targetSelector(null)).toBe('')
    expect(targetSelector(undefined)).toBe('')
    expect(targetSelector({})).toBe('')
    expect(targetSelector({ testid: '' })).toBe('')
    expect(targetSelector({ testid: 42 })).toBe('')
  })

  it('refuses outright when the within scope cannot be built, rather than widening the query', () => {
    expect(targetSelector({ testid: 'guide-next', within: { testid: '' } })).toBe('')
    expect(targetSelector({ testid: 'guide-next', within: {} })).toBe('')
  })
})

describe('resolveTarget', () => {
  it('queries with the built selector and hands back the element', () => {
    const el = { tag: 'button' }
    const asked = []
    const queryFn = (sel) => {
      asked.push(sel)
      return el
    }
    expect(resolveTarget({ testid: 'guide-next' }, queryFn)).toBe(el)
    expect(asked).toEqual(['[data-testid="guide-next"]'])
  })

  it('answers null for a null target without ever querying', () => {
    const queryFn = () => {
      throw new Error('should not be called')
    }
    expect(resolveTarget(null, queryFn)).toBe(null)
    expect(resolveTarget({ testid: '' }, queryFn)).toBe(null)
  })

  it('coerces a missing element to null', () => {
    expect(resolveTarget({ testid: 'guide-next' }, () => undefined)).toBe(null)
    expect(resolveTarget({ testid: 'guide-next' }, () => null)).toBe(null)
  })

  it('refuses instead of throwing when the query itself throws', () => {
    const queryFn = () => {
      throw new Error('document torn down')
    }
    expect(resolveTarget({ testid: 'guide-next' }, queryFn)).toBe(null)
  })

  it('re-resolves every call, never caching from offer time', () => {
    // The same target against a document that changed between calls: the
    // second answer must reflect the change, or the pointer points at a ghost.
    const target = { testid: 'board-well-part', attrs: { ref: 'C1' } }
    const el = { tag: 'g' }
    let present = true
    const queryFn = () => (present ? el : null)
    expect(resolveTarget(target, queryFn)).toBe(el)
    present = false
    expect(resolveTarget(target, queryFn)).toBe(null)
  })
})

describe('guide state machine', () => {
  it('starts idle', () => {
    expect(get(guide)).toMatchObject({ phase: 'idle', steps: [], step: -1 })
  })

  it('offering moves to offered without pointing at anything yet', () => {
    expect(offerGuide(twoSteps, 'Walk through C1?')).toBe(true)
    const state = get(guide)
    expect(state.phase).toBe('offered')
    expect(state.step).toBe(-1)
    expect(state.label).toBe('Walk through C1?')
    expect(activeStep(state)).toBe(null)
  })

  it('keeps steps as data — targets stay declarative, no element is held', () => {
    offerGuide(twoSteps)
    const state = get(guide)
    expect(state.steps[1].target).toEqual({ testid: 'board-well-part', attrs: { ref: 'C1' } })
    expect(state.steps[1].tab).toBe('board')
  })

  it('normalizes a caption-only step to target null and keeps it', () => {
    offerGuide([{ target: null, caption: 'Nothing to point at.' }])
    expect(get(guide).steps[0].target).toBe(null)
  })

  it('drops a step with no caption — a captionless step has no honest degraded form', () => {
    offerGuide([{ target: { testid: 'x' }, caption: '' }, { caption: 'Only this survives.' }])
    expect(get(guide).steps).toHaveLength(1)
  })

  it('refuses an empty offer and stays idle', () => {
    expect(offerGuide([])).toBe(false)
    expect(offerGuide([{ caption: '   ' }])).toBe(false)
    expect(get(guide).phase).toBe('idle')
  })

  it('starts only from offered — an out-of-band start is ignored, not obeyed', () => {
    startGuide()
    expect(get(guide).phase).toBe('idle')
    offerGuide(twoSteps)
    startGuide()
    const state = get(guide)
    expect(state.phase).toBe('step')
    expect(state.step).toBe(0)
    expect(activeStep(state)).toBe(state.steps[0])
  })

  it('advances step by step and finishes past the last', () => {
    offerGuide(twoSteps)
    startGuide()
    expect(isLastStep(get(guide))).toBe(false)
    nextStep()
    expect(get(guide).step).toBe(1)
    expect(isLastStep(get(guide))).toBe(true)
    nextStep()
    expect(get(guide)).toMatchObject({ phase: 'done', steps: [], step: -1 })
  })

  it('dismisses from the offer and from mid-step alike', () => {
    offerGuide(twoSteps)
    dismissGuide('declined')
    expect(get(guide).phase).toBe('dismissed')

    offerGuide(twoSteps)
    startGuide()
    dismissGuide()
    expect(get(guide).phase).toBe('dismissed')
  })

  it('ignores next and dismiss when nothing is running', () => {
    nextStep()
    dismissGuide()
    expect(get(guide).phase).toBe('idle')
  })

  it('resets to idle silently for run teardown', () => {
    offerGuide(twoSteps)
    startGuide()
    resetGuide()
    expect(get(guide)).toMatchObject({ phase: 'idle', steps: [], step: -1 })
  })
})

describe('applyDeskResolve', () => {
  it('offers and starts a one-step guide from a desk target', () => {
    expect(
      applyDeskResolve({
        caption: 'That blocker is about C1.',
        abstain: false,
        target: { testid: 'finding-card', attrs: { sev: 'blocker' }, tab: 'review' },
      }),
    ).toBe(true)
    const state = get(guide)
    expect(state.phase).toBe('step')
    expect(state.steps[0].caption).toBe('That blocker is about C1.')
    expect(state.steps[0].target).toEqual({
      testid: 'finding-card',
      attrs: { sev: 'blocker' },
    })
    expect(state.steps[0].tab).toBe('review')
  })

  it('stays caption-only when the model abstains', () => {
    applyDeskResolve({
      caption: 'I cannot tell.',
      abstain: true,
      target: { testid: 'finding-card' },
    })
    expect(get(guide).steps[0].target).toBe(null)
  })

  it('refuses an empty caption', () => {
    expect(applyDeskResolve({ caption: '  ', abstain: false, target: null })).toBe(false)
    expect(get(guide).phase).toBe('idle')
  })
})

describe('findingGuideSteps', () => {
  const finding = {
    severity: 'marginal',
    title: 'Raise C1 to 22uF',
    parts: ['C1', 'U1'],
  }

  it('builds two steps: the selected card\'s show-on-board button, then the first named part', () => {
    const steps = findingGuideSteps(finding)
    expect(steps).toHaveLength(2)
    expect(targetSelector(steps[0].target)).toBe(
      '[data-testid="finding-card"][data-selected="true"] [data-testid="finding-card-show-board"]',
    )
    expect(steps[1].target).toEqual({ testid: 'board-well-part', attrs: { ref: 'C1' } })
    expect(steps[1].caption).toContain('C1')
  })

  it('switches to the board tab on the second step, not the first', () => {
    const steps = findingGuideSteps(finding)
    expect(steps[0].tab ?? '').toBe('')
    expect(steps[1].tab).toBe('board')
  })

  it('degrades to a caption-only second step when the finding names no parts', () => {
    const steps = findingGuideSteps({ title: 'General note', parts: [] })
    expect(steps).toHaveLength(2)
    expect(steps[1].target).toBe(null)
    expect(steps[1].caption).toContain('nothing to point at')
  })

  it('skips empty part entries when picking the ref', () => {
    const steps = findingGuideSteps({ title: 't', parts: ['', null, 'R3'] })
    expect(steps[1].target).toEqual({ testid: 'board-well-part', attrs: { ref: 'R3' } })
  })
})
