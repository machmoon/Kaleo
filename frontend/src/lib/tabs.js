// Which pane the centre column shows. The name lives in the hash fragment so a
// tab switch never reaches the server, and App is the only thing that reads it.

export const TABS = ['chat', 'schematic', 'placement', 'board', 'case', 'sourcing', 'review']

/** The tab a hash names, or '' when it names none of them. */
export function parseTab(hash) {
  const name = String(hash ?? '')
    .replace(/^#/, '')
    .trim()
    .toLowerCase()
  return TABS.includes(name) ? name : ''
}

/** The tab actually shown. Drawing tabs fall back to the durable transcript
    until the current run carries a contract each renderer accepts. */
export function resolveTab(
  hash,
  // `case` is a reserved word, so the binding is renamed; callers still pass
  // the flag under the tab's own name, like every other entry here.
  {
    schematic = false,
    placement = false,
    board = false,
    review = false,
    case: caseTab = false,
    sourcing = false,
  } = {},
) {
  const name = parseTab(hash)
  if (name === 'schematic' && schematic) return 'schematic'
  if (name === 'placement' && placement) return 'placement'
  if (name === 'board' && board) return 'board'
  if (name === 'case' && caseTab) return 'case'
  if (name === 'sourcing' && sourcing) return 'sourcing'
  if (name === 'review' && review) return 'review'
  return 'chat'
}
