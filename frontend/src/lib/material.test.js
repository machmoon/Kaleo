import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

function source(relative) {
  return readFileSync(new URL(relative, import.meta.url), 'utf8')
}

describe('material surface contract', () => {
  it('keeps paper behavior explicit and lets glass override the same tokens', () => {
    const tokens = source('../styles/tokens.css')
    const glass = source('../styles/glass.css')
    const base = source('../styles/base.css')

    expect(tokens).toContain('--material-filter: none;')
    expect(tokens).toContain('--panel-radius: 0;')
    expect(tokens).toContain('--sticky-surface: var(--paper);')
    expect(glass).toContain('--material-filter: saturate(150%) blur(22px);')
    expect(glass).toContain('--sticky-surface: light-dark(')
    expect(base).toContain("[data-material='chrome']")
    expect(base).toContain("[data-material='sticky']")
  })

  it('has no component-specific legacy glass blur hooks', () => {
    const files = [
      '../styles/base.css',
      '../styles/glass.css',
      '../components/TitleBar.svelte',
      '../components/StatusBar.svelte',
      '../components/SideRail.svelte',
      '../components/DebugConsole.svelte',
      '../components/IntentForm.svelte',
      '../components/FindingCard.svelte',
    ]

    for (const file of files) expect(source(file)).not.toContain('--glass-blur')
  })

  it('classifies chrome and every current high-level content surface', () => {
    const chrome = [
      ['../components/TitleBar.svelte', /data-testid="title-bar" data-material="chrome"/],
      ['../components/StatusBar.svelte', /data-testid="status-bar" data-material="chrome"/],
      ['../components/SideRail.svelte', /data-testid="side-rail" data-material="chrome"/],
      ['../components/DebugConsole.svelte', /data-testid="debug-console"\s+data-material="chrome"/],
    ]
    const content = [
      ['../components/IntentForm.svelte', /data-testid="intent-form-intent"\s+data-material="panel"/],
      ['../components/IntentForm.svelte', /data-testid="intent-form-orchestrator" data-material="panel"/],
      ['../components/ConversationView.svelte', /class="reply"[^>]+data-material="sticky"/],
      ['../components/ConversationView.svelte', /data-testid="chat-recovery" data-material="panel"/],
      ['../components/ActivityCard.svelte', /data-testid="chat-activity"[^>]+data-material="panel"/],
      ['../components/FindingCard.svelte', /data-testid="finding-card" data-material="panel"/],
      ['../components/ReviewResults.svelte', /data-testid="review-results-state"[^>]+data-material="panel"/],
      ['../components/BoardWell.svelte', /data-testid="board-well-tip" data-material="popover"/],
      ['../components/SchematicWell.svelte', /class="caption" data-material="panel"/],
      ['../components/PlacementBoard.svelte', /data-testid="placement-board"[^>]+data-material="panel"/],
      ['../components/MetricHelp.svelte', /class="tooltip"[^>]+data-material="popover"/],
      ['../components/PlacementLab.svelte', /class="controls"[^>]+data-material="panel"/],
      ['../components/PlacementLab.svelte', /class="trace"[^>]+data-material="panel"/],
      ['../components/CaseTab.svelte', /class="receipt" data-material="panel"/],
      ['../components/CaseTab.svelte', /class="empty" data-material="panel"/],
      ['../components/SourcingTab.svelte', /class="receipt" data-material="panel"/],
      ['../components/SourcingTab.svelte', /class="empty" data-material="panel"/],
    ]

    for (const [file, pattern] of [...chrome, ...content]) {
      expect(source(file), `${file} is missing its material role`).toMatch(pattern)
    }

    // One per artifact card: schematic, board, placement, case, sourcing, review.
    expect(source('../components/ArtifactCards.svelte').match(/data-material="panel"/g)).toHaveLength(6)
  })

  it('marks both drawing wells as intentional canvas exemptions', () => {
    expect(source('../components/BoardWell.svelte')).toMatch(/class="frame" data-material="canvas"/)
    expect(source('../components/SchematicWell.svelte')).toMatch(/class="frame" data-material="canvas"/)
    expect(source('../components/PlacementBoard.svelte')).toMatch(/class="well" data-material="canvas"/)
  })
})
