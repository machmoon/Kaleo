// The one place a severity string becomes something on screen.

const TIERS = {
  blocker: { key: 'blocker', label: 'will not work', rank: 0 },
  marginal: { key: 'marginal', label: 'marginal', rank: 1 },
  note: { key: 'note', label: 'note', rank: 2 },
}

export const SEVERITY_ORDER = ['blocker', 'marginal', 'note']

/** An unrecognised severity keeps its own words but is styled as a note, never as a blocker. */
export function severityInfo(raw) {
  const key = String(raw ?? '').toLowerCase()
  if (TIERS[key]) return TIERS[key]
  return { key: 'note', label: key || 'note', rank: 2 }
}

export function countBySeverity(findings) {
  const counts = { blocker: 0, marginal: 0, note: 0 }
  for (const finding of findings || []) counts[severityInfo(finding.severity).key] += 1
  return counts
}
