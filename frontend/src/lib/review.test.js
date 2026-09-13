// The review block is the one fact the finding list cannot carry: whether an
// empty list means "clean" or "nothing is known". These pin the three words
// against the shape service/steps.py::review_block puts on the wire.

import { describe, expect, it } from 'vitest'
import { reviewReason, reviewStatus } from './review.js'

describe('reviewStatus', () => {
  it('reads the three statuses the service sends, and absent for none', () => {
    expect(reviewStatus({ review: { status: 'ok', ran: true, detail: null, note: '' } })).toBe('ok')
    expect(
      reviewStatus({ review: { status: 'failed', ran: true, detail: 'not JSON', note: 'x' } }),
    ).toBe('failed')
    expect(
      reviewStatus({ review: { status: 'skipped', ran: false, detail: null, note: 'x' } }),
    ).toBe('skipped')
    expect(reviewStatus({ findings: [] })).toBe('absent')
    expect(reviewStatus(null)).toBe('absent')
  })

  it('treats a malformed block as absent and an unknown status as ok', () => {
    expect(reviewStatus({ review: 'failed' })).toBe('absent')
    expect(reviewStatus({ review: ['failed'] })).toBe('absent')
    expect(reviewStatus({ review: { status: 'green' } })).toBe('ok')
  })
})

describe('reviewReason', () => {
  it('prefers the detail, then the note, then a fixed sentence', () => {
    expect(
      reviewReason({ review: { status: 'failed', detail: 'not JSON', note: 'the review failed' } }),
    ).toBe('not JSON')
    expect(reviewReason({ review: { status: 'failed', detail: null, note: 'the review failed' } })).toBe(
      'the review failed',
    )
    expect(reviewReason({ review: { status: 'failed', detail: '', note: '' } })).toBe(
      'the critic answered nothing readable',
    )
  })

  it('has nothing to say about an ok or absent review', () => {
    expect(reviewReason({ review: { status: 'ok', detail: null, note: '' } })).toBe('')
    expect(reviewReason({ findings: [] })).toBe('')
  })
})
