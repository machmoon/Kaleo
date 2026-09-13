// The SPA's read of the service's additive `review` block, which mirrors
// `ReviewReport.as_dict()` in engine/silkscreen/agents/review.py as wrapped by
// `service/steps.py::review_block`: `status` (`ok` / `failed` / `skipped`),
// `ran`, `detail` (null when there is nothing to say) and `note`.
//
// Three outcomes stay three words, because an empty finding list means "the
// critic found nothing" only when the block says `ok`. A critic that was asked
// and answered nothing readable also leaves an empty list, and "Nothing to
// flag" over that is the clean-board reading this module exists to refuse. An
// older service sends no block; its list is then read as before, which is what
// `absent` is for -- the caller falls back to the request's own `review` flag.

function objectOf(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : null
}

/** `ok`, `failed`, `skipped`, or `absent` when the response carries no block.
    Any other status the wire ever grows reads as `ok`, the pre-block reading,
    so a new success word cannot hide findings. */
export function reviewStatus(result) {
  const block = objectOf(result?.review)
  if (!block) return 'absent'
  if (block.status === 'failed') return 'failed'
  if (block.status === 'skipped') return 'skipped'
  return 'ok'
}

/** The engine's own sentence about a review that did not answer: `detail`
    when it has one, else `note`, else a fixed fallback. Empty for `ok` and
    `absent`, because there is nothing to explain. */
export function reviewReason(result) {
  const status = reviewStatus(result)
  if (status === 'ok' || status === 'absent') return ''
  const block = objectOf(result.review)
  const detail = typeof block.detail === 'string' ? block.detail.trim() : ''
  const note = typeof block.note === 'string' ? block.note.trim() : ''
  return detail || note || 'the critic answered nothing readable'
}
