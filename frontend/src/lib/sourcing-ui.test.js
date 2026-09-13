import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

function source(relative) {
  return readFileSync(new URL(relative, import.meta.url), 'utf8')
}

// The sourcing contract at the UI's altitude: the opt-in is off by default and
// says what it costs, the tab is hash-addressed like Case, a datasheet becomes
// a link only on `verified`, and no surface calls an MPN anything but proposed.
describe('sourcing form contract', () => {
  it('defaults sourcing off and keeps a restored request\'s own answer', () => {
    const form = source('../components/IntentForm.svelte')

    expect(form).toContain('let sourcing = $state(seed.sourcing === true)')
    expect(form).toContain('data-testid="intent-form-sourcing"')
  })

  it('says in the label that sourcing costs a model call and datasheet probes', () => {
    const form = source('../components/IntentForm.svelte')

    expect(form).toContain('Source the parts')
    expect(form).toContain('one more model call + datasheet probes')
  })
})

describe('sourcing tab contract', () => {
  const tab = source('../components/SourcingTab.svelte')

  it('links a datasheet only through canOpenDatasheet, never off the status alone', () => {
    expect(tab).toContain('{#if canOpenDatasheet(row)}')
    expect(tab).toMatch(/<a[^>]*href=\{row\.datasheetUrl\}[^>]*rel="noopener noreferrer"/)
    // One anchor on the page, and it sits inside that branch.
    expect(tab.match(/<a\b/g)).toHaveLength(1)
  })

  it('labels MPNs as proposals and never as verified', () => {
    expect(tab).toContain('data-testid="sourcing-mpn-status"')
    expect(tab).toContain('proposed, not verified')
    expect(tab.toLowerCase()).not.toMatch(/mpn[^.]*\bverified mpn\b/)
  })

  it('shows the unmatched 3D model note in the model column', () => {
    expect(tab).toContain("{row.model3dNote || 'no library model'}")
    expect(tab).toContain('data-matched="false"')
  })

  it('keeps ids on intrinsic elements with data-ref as the row identity', () => {
    expect(tab).toMatch(/<tr data-testid="sourcing-row" data-ref=\{row\.ref\}/)
    expect(tab).not.toMatch(/data-testid="sourcing-row-\$\{/)
  })

  it('offers the BOM download as the primary action', () => {
    expect(tab).toMatch(/class="primary"[^>]*data-testid="sourcing-download"/)
  })

  it('uses none of the words the product vocabulary forbids', () => {
    for (const file of [
      '../components/SourcingTab.svelte',
      '../components/IntentForm.svelte',
      './sourcing.js',
    ]) {
      expect(source(file).toLowerCase()).not.toMatch(/\bhelper\b|\bassistant\b/)
    }
  })
})

describe('sourcing tab wiring', () => {
  it('is hash-addressed through the status bar and rendered by App', () => {
    const bar = source('../components/StatusBar.svelte')
    const app = source('../App.svelte')

    expect(bar).toContain('href="#sourcing"')
    expect(bar).toContain('data-testid="status-bar-tab-sourcing"')
    expect(app).toContain("tab === 'sourcing' && sourcingEnabled")
    expect(app).toContain("requested={$run.request?.sourcing === true}")
  })

  it('has an artifact card that counts proposals, not confirmations', () => {
    const cards = source('../components/ArtifactCards.svelte')

    expect(cards).toContain("onopen?.('sourcing')")
    expect(cards).toContain('MPNs proposed')
    expect(cards).not.toContain('MPNs verified')
  })
})
