// Which skin tokens.css and glass.css resolve to. Deliberately shaped like
// theme.js -- decisions about a value, never a write, so vitest can run this
// in node and the one line that touches the document lives with the toggle in
// TitleBar and the boot in main.js.
//
// Skin and theme are orthogonal, and the pairing is the point: skin picks the
// material (paper or glass), theme picks the light it is read under. Both
// skins carry a full light-dark() palette, so all four combinations are real.
//
// Unlike theme, absence here is not a third state waiting on the OS -- nothing
// outside the app has an opinion about which material it should be -- so no
// stored choice simply means the Drafting Table default.

export const STORAGE_KEY = 'silkscreen-skin'
export const DEFAULT_SKIN = 'paper'

/** A skin name, or null for anything else. localStorage hands back whatever
    is under the key -- a stale value, another tab's typo, null. */
export function normalizeSkin(value) {
  return value === 'glass' || value === 'paper' ? value : null
}

/** The stored choice. A storage that is missing, or throws because the browser
    blocks it, reads as no choice rather than an error. */
export function readStored(storage) {
  try {
    return normalizeSkin(storage.getItem(STORAGE_KEY))
  } catch {
    return null
  }
}

/** Persist a choice. Returns whether it stuck: a full or blocked storage is
    not worth failing a click over, but it is worth not lying about. */
export function writeStored(storage, skin) {
  const choice = normalizeSkin(skin)
  if (!choice) return false
  try {
    storage.setItem(STORAGE_KEY, choice)
    return true
  } catch {
    return false
  }
}

/** The skin actually showing, which is what the toggle flips away from. */
export function resolveSkin(stored) {
  return normalizeSkin(stored) || DEFAULT_SKIN
}

export function toggleSkin(current) {
  return current === 'glass' ? 'paper' : 'glass'
}

/** The `data-skin` value to write, or null for the default, meaning leave the
    attribute off. Glass is the whole of the override block's selector, so an
    absent attribute is exactly Drafting Table -- there is nothing to unset. */
export function skinAttribute(stored) {
  const skin = resolveSkin(stored)
  return skin === DEFAULT_SKIN ? null : skin
}
