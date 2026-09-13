import { describe, expect, it } from 'vitest'
import {
  DEFAULT_SKIN,
  STORAGE_KEY,
  normalizeSkin,
  readStored,
  resolveSkin,
  skinAttribute,
  toggleSkin,
  writeStored,
} from './skin.js'

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

describe('normalizeSkin', () => {
  it('passes the two skin names through', () => {
    expect(normalizeSkin('paper')).toBe('paper')
    expect(normalizeSkin('glass')).toBe('glass')
  })

  it('rejects anything else', () => {
    for (const value of [null, undefined, '', 'Glass', 'frosted', 'dark', 0]) {
      expect(normalizeSkin(value)).toBe(null)
    }
  })
})

describe('readStored', () => {
  it('reads a stored choice', () => {
    expect(readStored(fakeStorage({ [STORAGE_KEY]: 'glass' }))).toBe('glass')
  })

  it('reads an empty storage as no choice', () => {
    expect(readStored(fakeStorage())).toBe(null)
  })

  it('reads a junk value as no choice rather than passing it through', () => {
    expect(readStored(fakeStorage({ [STORAGE_KEY]: 'chrome' }))).toBe(null)
  })

  it('reads a blocked storage as no choice rather than throwing', () => {
    expect(readStored(blockedStorage)).toBe(null)
  })
})

describe('writeStored', () => {
  it('stores a choice and reports that it stuck', () => {
    const storage = fakeStorage()
    expect(writeStored(storage, 'glass')).toBe(true)
    expect(storage.map[STORAGE_KEY]).toBe('glass')
  })

  it('refuses a value that is not a skin, and writes nothing', () => {
    const storage = fakeStorage()
    expect(writeStored(storage, 'frosted')).toBe(false)
    expect(STORAGE_KEY in storage.map).toBe(false)
  })

  it('reports a blocked storage rather than throwing', () => {
    expect(writeStored(blockedStorage, 'glass')).toBe(false)
  })
})

describe('resolveSkin', () => {
  it('shows a stored choice', () => {
    expect(resolveSkin('glass')).toBe('glass')
    expect(resolveSkin('paper')).toBe('paper')
  })

  it('falls back to the default when nothing is stored', () => {
    expect(resolveSkin(null)).toBe(DEFAULT_SKIN)
    expect(resolveSkin('nonsense')).toBe(DEFAULT_SKIN)
  })
})

describe('toggleSkin', () => {
  it('flips between the two skins', () => {
    expect(toggleSkin('glass')).toBe('paper')
    expect(toggleSkin('paper')).toBe('glass')
  })

  it('treats anything unrecognised as not-glass, so the flip lands on glass', () => {
    expect(toggleSkin(null)).toBe('glass')
  })
})

describe('skinAttribute', () => {
  it('writes the attribute for glass', () => {
    expect(skinAttribute('glass')).toBe('glass')
  })

  // The override block is keyed on [data-skin='glass'] alone, so the default
  // is the absence of the attribute rather than a value that unsets it.
  it('leaves the attribute off for the default skin', () => {
    expect(skinAttribute('paper')).toBe(null)
    expect(skinAttribute(null)).toBe(null)
  })
})
