import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

function source(relative) {
  return readFileSync(new URL(relative, import.meta.url), 'utf8')
}

// The demo-first enclosure contract: a fresh form defaults the case ON while a
// restored request keeps what it actually said, and rigor stays a second
// opt-in toggle rather than a hard-coded behavior.
describe('enclosure form contract', () => {
  it('defaults the case on for a fresh form and respects a restored explicit false', () => {
    const form = source('../components/IntentForm.svelte')

    expect(form).toContain('let enclosure = $state(initial ? seed.enclosure === true : true)')
  })

  it('offers rigorous fit checks as an off-by-default toggle shown with the case', () => {
    const form = source('../components/IntentForm.svelte')

    expect(form).toContain("let enclosureRigorous = $state(seed.enclosure_rigorous === true)")
    expect(form).toContain('data-testid="intent-form-enclosure-rigorous"')
    expect(form).toContain('rigorous fit checks (slower)')
  })

  it('names the engine and lists kernel clauses on intrinsic elements, one id per row', () => {
    const tab = source('../components/CaseTab.svelte')

    expect(tab).toContain('data-testid="case-engine"')
    expect(tab).toContain('data-testid="case-kernel"')
    expect(tab).toContain('data-testid="case-clause"')
    expect(tab).toContain('data-clause={clause.name}')
    expect(tab).toContain('data-testid="case-kernel-warning"')
    // A clause row is one <tr>, not a component tag; the id must sit on it.
    expect(tab).toMatch(/<tr[^>]*data-testid="case-clause"/)
    // No kernel report reads as "no kernel check", never as a pass.
    expect(tab).toContain('no kernel check')
    expect(tab).not.toMatch(/assistant|helper/i)
  })

  it('promotes the download path in the case tab with the STEP hint', () => {
    const tab = source('../components/CaseTab.svelte')

    expect(tab).toContain('data-testid="case-hint"')
    expect(tab).toContain('ISO 10303-21')
    expect(tab).toMatch(/class="primary"[^>]*data-testid="case-download"/)
  })

  // The source panel, the copy button and the download button all read the
  // STEP text now. The .scad field left the response on 2026-09-08, so a tab
  // still reading it would render undefined — this pins the vocabulary.
  it('reads the STEP assembly, not a retired .scad field', () => {
    const tab = source('../components/CaseTab.svelte')

    expect(tab).toContain('data-testid="case-step"')
    expect(tab).toContain('enclosure.step')
    expect(tab).toContain('STEP_FILENAME')
    expect(tab).not.toMatch(/scad/i)
  })

  // A STEP assembly runs to tens of thousands of lines; an uncapped <pre>
  // would stretch the tab to the length of the file.
  it('caps the source panel height so a long assembly scrolls rather than janks', () => {
    const tab = source('../components/CaseTab.svelte')

    expect(tab).toMatch(/pre \{[^}]*max-height:[^}]*overflow-y: auto/)
  })
})
