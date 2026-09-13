import { describe, expect, it } from 'vitest'
import {
  DARK_QUERY,
  STORAGE_KEY,
  normalizeTheme,
  prefersDark,
  readStored,
  resolveTheme,
  themeAttribute,
  toggleTheme,
  writeStored,
} from './theme.js'

function fakeStorage(initial = {}) {
  const map = { ...initial }
  return {
    map,
    getItem: (key) => (key in map ? map[key] : null),
    setItem: (key, value) => {
      map[key] = String(value)
    },
  }
}

const blockedStorage = {
  getItem() {
    throw new Error('blocked')
  },
  setItem() {
    throw new Error('blocked')
  },
}

describe('normalizeTheme', () => {
  it('passes the two theme names through', () => {
    expect(normalizeTheme('light')).toBe('light')
    expect(normalizeTheme('dark')).toBe('dark')
  })

  it('rejects anything else', () => {
    expect(normalizeTheme('')).toBe(null)
    expect(normalizeTheme('DARK')).toBe(null)
    expect(normalizeTheme('night')).toBe(null)
    expect(normalizeTheme(null)).toBe(null)
    expect(normalizeTheme(undefined)).toBe(null)
    expect(normalizeTheme(1)).toBe(null)
    expect(normalizeTheme({})).toBe(null)
  })
})

describe('readStored', () => {
  it('reads a stored choice', () => {
    expect(readStored(fakeStorage({ [STORAGE_KEY]: 'dark' }))).toBe('dark')
    expect(readStored(fakeStorage({ [STORAGE_KEY]: 'light' }))).toBe('light')
  })

  it('reads an empty storage as no choice', () => {
    expect(readStored(fakeStorage())).toBe(null)
  })

  it('reads a garbage value as no choice', () => {
    expect(readStored(fakeStorage({ [STORAGE_KEY]: 'midnight' }))).toBe(null)
    expect(readStored(fakeStorage({ [STORAGE_KEY]: '{"theme":"dark"}' }))).toBe(null)
  })

  it('survives a storage that is missing or throws', () => {
    expect(readStored(undefined)).toBe(null)
    expect(readStored(null)).toBe(null)
    expect(readStored(blockedStorage)).toBe(null)
  })
})

describe('writeStored', () => {
  it('stores a choice under the documented key', () => {
    const storage = fakeStorage()
    expect(writeStored(storage, 'dark')).toBe(true)
    expect(storage.map[STORAGE_KEY]).toBe('dark')
    expect(STORAGE_KEY).toBe('silkscreen-theme')
  })

  it('refuses to store anything that is not a theme', () => {
    const storage = fakeStorage()
    expect(writeStored(storage, 'night')).toBe(false)
    expect(writeStored(storage, null)).toBe(false)
    expect(STORAGE_KEY in storage.map).toBe(false)
  })

  it('reports a blocked storage rather than throwing', () => {
    expect(writeStored(blockedStorage, 'dark')).toBe(false)
    expect(writeStored(undefined, 'dark')).toBe(false)
  })
})

describe('prefersDark', () => {
  it('asks the OS through the documented query', () => {
    let asked = ''
    const view = {
      matchMedia(query) {
        asked = query
        return { matches: true }
      },
    }
    expect(prefersDark(view)).toBe(true)
    expect(asked).toBe(DARK_QUERY)
  })

  it('is false when the OS wants light', () => {
    expect(prefersDark({ matchMedia: () => ({ matches: false }) })).toBe(false)
  })

  it('is false where matchMedia is missing or throws', () => {
    expect(prefersDark({})).toBe(false)
    expect(prefersDark(undefined)).toBe(false)
    expect(
      prefersDark({
        matchMedia() {
          throw new Error('no')
        },
      }),
    ).toBe(false)
  })
})

describe('resolveTheme', () => {
  it('takes the stored choice over the OS, in both directions', () => {
    expect(resolveTheme('light', true)).toBe('light')
    expect(resolveTheme('dark', false)).toBe('dark')
  })

  it('follows the OS when nothing is stored', () => {
    expect(resolveTheme(null, true)).toBe('dark')
    expect(resolveTheme(null, false)).toBe('light')
  })

  it('follows the OS when the stored value is garbage', () => {
    expect(resolveTheme('midnight', true)).toBe('dark')
    expect(resolveTheme('', false)).toBe('light')
  })
})

describe('toggleTheme', () => {
  it('flips between the two themes', () => {
    expect(toggleTheme('dark')).toBe('light')
    expect(toggleTheme('light')).toBe('dark')
  })

  it('treats anything unresolved as light, so the first click goes dark', () => {
    expect(toggleTheme(null)).toBe('dark')
    expect(toggleTheme('midnight')).toBe('dark')
  })
})

describe('themeAttribute', () => {
  it('is the stored choice', () => {
    expect(themeAttribute('dark')).toBe('dark')
    expect(themeAttribute('light')).toBe('light')
  })

  it('is null with no choice, so the attribute stays off and the OS decides', () => {
    expect(themeAttribute(null)).toBe(null)
    expect(themeAttribute('midnight')).toBe(null)
  })
})
