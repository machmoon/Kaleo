const DOT = '·'

export function formatDuration(seconds) {
  const s = Math.max(0, Math.round(Number(seconds) || 0))
  if (s < 60) return `${s} s`
  return `${Math.floor(s / 60)} m ${String(s % 60).padStart(2, '0')} s`
}

/** The pipeline feed's time column: bare seconds under a minute, m:ss above.
    Narrow on purpose, because it repeats down every row — formatDuration is the
    prose form. An absent time renders as nothing rather than as zero. */
export function formatClock(seconds) {
  const n = Number(seconds)
  if (seconds == null || seconds === '' || !Number.isFinite(n)) return ''
  const s = Math.max(0, Math.round(n))
  if (s < 60) return `${s} s`
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

export function formatCount(n, singular, plural = `${singular}s`) {
  return `${n} ${n === 1 ? singular : plural}`
}

export function formatParts(parts) {
  return (parts || []).filter(Boolean).join(` ${DOT} `)
}

export function formatBoard(boardMm) {
  const [w, h] = boardMm || []
  if (typeof w !== 'number' || typeof h !== 'number') return ''
  return `${w.toFixed(1)} × ${h.toFixed(1)} mm`
}

/** Half-perimeter wirelength, the placer's objective. Absent on a response
    that carried no board, so a missing value renders as nothing at all. */
export function formatWirelength(mm) {
  const n = Number(mm)
  if (mm == null || !Number.isFinite(n)) return ''
  return `wirelength ${n.toFixed(1)} mm`
}

/** requirements/pins arrive as either a count or the list itself. */
export function countOf(value) {
  if (Array.isArray(value)) return value.length
  const n = Number(value)
  return Number.isFinite(n) ? n : 0
}

export function joinDot(parts) {
  return parts.filter(Boolean).join(` ${DOT} `)
}
