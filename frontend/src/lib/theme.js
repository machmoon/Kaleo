// Which palette tokens.css resolves to. Everything here is a decision about a
// value, never a write: the storage and the media query are passed in, so
// vitest can run this in node, and the one line that touches the document
// lives with the toggle in TitleBar and the boot in main.js.
//
// A stored theme is an explicit choice and outranks the OS. No stored theme is
// not "light" — it is the absence of a choice, and it must stay absent so the
// stylesheet's media query keeps following the OS as it changes.

export const STORAGE_KEY = 'silkscreen-theme'
export const DARK_QUERY = '(prefers-color-scheme: dark)'

/** A theme name, or null for anything else. localStorage hands back whatever
    is under the key -- a stale value, another tab's typo, null. */
export function normalizeTheme(value) {
  return value === 'dark' || value === 'light' ? value : null
}

/** The stored choice. A storage that is missing, or throws because the browser
    blocks it, reads as no choice rather than an error. */
export function readStored(storage) {
  try {
    return normalizeTheme(storage.getItem(STORAGE_KEY))
  } catch {
    return null
  }
}

/** Persist a choice. Returns whether it stuck: a full or blocked storage is
    not worth failing a click over, but it is worth not lying about. */
export function writeStored(storage, theme) {
  const choice = normalizeTheme(theme)
  if (!choice) return false
  try {
    storage.setItem(STORAGE_KEY, choice)
    return true
  } catch {
    return false
  }
}

/** What the OS asks for. `view` is a window; anything without matchMedia --
    an older browser, a test -- reads as light. */
export function prefersDark(view) {
  try {
    return Boolean(view.matchMedia(DARK_QUERY).matches)
  } catch {
    return false
  }
}

/** The theme actually showing, which is what the toggle flips away from. */
export function resolveTheme(stored, osPrefersDark) {
  return normalizeTheme(stored) || (osPrefersDark ? 'dark' : 'light')
}

export function toggleTheme(current) {
  return current === 'dark' ? 'light' : 'dark'
}

/** The `data-theme` value for a stored choice, or null meaning leave the
    attribute off and let the media query decide. */
export function themeAttribute(stored) {
  return normalizeTheme(stored)
}
