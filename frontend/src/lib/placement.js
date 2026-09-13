function objectOf(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : null
}

function finite(value) {
  return typeof value === 'number' && Number.isFinite(value)
}

function componentOf(value) {
  const component = objectOf(value)
  return Boolean(
    component
      && typeof component.ref === 'string'
      && component.ref.trim()
      && finite(component.x)
      && finite(component.y)
      && finite(component.width)
      && component.width > 0
      && finite(component.height)
      && component.height > 0
      && finite(component.angle),
  )
}

function keepoutOf(value) {
  const keepout = objectOf(value)
  return Boolean(
    keepout
      && typeof keepout.name === 'string'
      && keepout.name.trim()
      && finite(keepout.x)
      && finite(keepout.y)
      && finite(keepout.width)
      && keepout.width > 0
      && finite(keepout.height)
      && keepout.height > 0,
  )
}

function boardOf(value) {
  const board = objectOf(value)
  const components = Array.isArray(board?.components) ? board.components : []
  const keepouts = Array.isArray(board?.keepouts) ? board.keepouts : []
  return Boolean(
    board
      && finite(board.width)
      && board.width > 0
      && finite(board.height)
      && board.height > 0
      && Array.isArray(board.components)
      && components.every(componentOf)
      && new Set(components.map((component) => component.ref)).size === components.length
      && (board.keepouts === undefined || Array.isArray(board.keepouts))
      && keepouts.every(keepoutOf)
      && new Set(keepouts.map((keepout) => keepout.name)).size === keepouts.length,
  )
}

function receiptOf(value) {
  const receipt = objectOf(value)
  const action = objectOf(receipt?.action)
  return Boolean(
    receipt
      && action
      && typeof action.kind === 'string'
      && action.kind.trim()
      && typeof action.ref === 'string'
      && action.ref.trim()
      && finite(action.x)
      && finite(action.y)
      && finite(receipt.hard_before)
      && finite(receipt.hard_after),
  )
}

function stepOf(value) {
  const step = objectOf(value)
  return Boolean(
    step
      && (step.receipts === undefined
        || (Array.isArray(step.receipts) && step.receipts.every(receiptOf))),
  )
}

function evaluationOf(value) {
  const evaluation = objectOf(value)
  return Boolean(evaluation && finite(evaluation.hard) && finite(evaluation.soft))
}

function scoreOf(value) {
  const score = objectOf(value)
  return Boolean(score && evaluationOf(score.before) && evaluationOf(score.after))
}

/**
 * Return the placement-repair artifact only when it satisfies the fields its
 * renderer needs. Session files are user-provided JSON, so they receive the
 * same defensive contract check as the schematic and board artifacts.
 */
export function readPlacementRepair(result) {
  const placement = objectOf(result)?.placement_repair
  if (!objectOf(placement)) return null
  if (!boardOf(placement.start) || !boardOf(placement.board)) return null
  if (!scoreOf(placement.score)) return null
  if (
    placement.steps !== undefined
    && (!Array.isArray(placement.steps) || !placement.steps.every(stepOf))
  ) return null
  return placement
}
