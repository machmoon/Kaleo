import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

function source(relative) {
  return readFileSync(new URL(relative, import.meta.url), 'utf8')
}

// The routing contract at the UI's altitude: every surface that describes a
// finished board reads the `routing` block through readRouting, says "not
// routed" when it is absent, and names each unrouted net on its own row.
describe('routing summary contract', () => {
  const summary = source('../components/RoutingSummary.svelte')

  it('puts its ids on intrinsic elements and one id per unrouted net, keyed by data-net', () => {
    expect(summary).toMatch(/<div[^>]*data-testid="routing-summary"/)
    expect(summary).toMatch(/<span[^>]*data-testid="routing-summary-line"/)
    expect(summary).toMatch(/<ul[^>]*data-testid="routing-unrouted"/)
    expect(summary).toMatch(/<li[^>]*data-testid="routing-unrouted-net"[^>]*data-net=\{entry\.net\}/)
    expect(summary).toMatch(/<span[^>]*data-testid="routing-unrouted-reason"/)
    // No index suffix anywhere: rows share one id and differ by net name.
    expect(summary).not.toMatch(/data-testid="routing-unrouted-net-\{/)
  })

  it('renders the reason verbatim and never says the board is ready over a ratsnest', () => {
    expect(summary).toContain('{entry.reason}')
    expect(summary).toContain('is not ready to order until they are routed by hand')
    expect(summary).not.toMatch(/board ready/i)
  })

  it('says "not routed" out loud when the block is absent and never shows 100% of nothing', () => {
    expect(summary).toContain("status === 'unrouted'")
    expect(summary).toContain('routing turned off')
    expect(summary).toContain("status === 'empty'")
    // The percentage is gated on routingPercent, which is '' with no fraction.
    expect(summary).toMatch(/\{#if percent\}/)
  })
})

describe('result surfaces read routing', () => {
  it('the board tab carries the routing receipt and puts the count in its caption', () => {
    const well = source('../components/BoardWell.svelte')
    const app = source('../App.svelte')

    expect(well).toContain("import RoutingSummary from './RoutingSummary.svelte'")
    expect(well).toContain('<RoutingSummary {routing} />')
    expect(well).toContain('routingSummary(routing)')
    expect(app).toContain('<BoardWell {placements} {highlightedRefs} {pcb} {routing} />')
    expect(app).toContain("readRouting($run.result)")
  })

  it('the review summary and receipt name the copper', () => {
    const review = source('../components/ReviewResults.svelte')

    expect(review).toContain('<RoutingSummary {routing} />')
    expect(review).toContain('routingSummary(routing)')
  })

  it('the PCB artifact card and the side rail say how much was routed', () => {
    const cards = source('../components/ArtifactCards.svelte')
    const rail = source('../components/SideRail.svelte')

    expect(cards).toMatch(/<span[^>]*data-testid="chat-artifact-routing"/)
    expect(cards).toContain('routingSummary(routing)')
    expect(rail).toContain("{ label: 'Nets routed', value: routingSummary(readRouting(result)) }")
  })

  it('the client-side completion sentence goes through runOutcomeText', () => {
    const run = source('./run.js')

    expect(run).toContain('text: runOutcomeText(result)')
    expect(run).not.toContain("'The board run completed.'")
  })
})
