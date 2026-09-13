import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

function source(relative) {
  return readFileSync(new URL(relative, import.meta.url), 'utf8')
}

describe('constraint component contract', () => {
  it('keeps constraints optional while requiring approval when enabled', () => {
    const form = source('../components/IntentForm.svelte')

    expect(form).toContain('let constraintsEnabled = $state(hasSeedConstraints)')
    expect(form).toContain('(!constraintsEnabled || (constraintsReady && constraints.approved === true))')
    expect(form).toContain('data-testid="intent-form-constraints-enabled"')
    expect(form).toContain('data-testid="intent-form-constraints-approved"')
    expect(form).toContain('invalidateConstraintApproval')
    expect(form).toContain('seedConstraints.approved = false')
    expect(form).toContain('removeConstraintClass')
    expect(form).toContain('removeKeepout')
    expect(form).toContain('removeFixedPlacement')
    expect(form).toContain('removeThermalPair')
  })

  it('renders blockers, mechanical checks, evidence, and raw JSON', () => {
    const results = source('../components/ReviewResults.svelte')

    expect(results).toContain('data-testid="constraint-receipt-blockers"')
    expect(results).toContain('Mechanical checks')
    expect(results).toContain('<summary>Evidence</summary>')
    expect(results).toContain('Raw constraint receipt JSON')
  })
})
