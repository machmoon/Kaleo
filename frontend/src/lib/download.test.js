import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { get } from 'svelte/store'
import { LOG_TEXT_MIME, clearLog, log } from './log.js'
import { PCB_FILENAME, PCB_MIME, downloadPcb, downloadText, pcbText } from './download.js'

describe('pcbText', () => {
  it('returns the board file the response carries', () => {
    expect(pcbText({ kicad_pcb: '(kicad_pcb (version 20240108))' })).toBe(
      '(kicad_pcb (version 20240108))',
    )
  })

  it('returns nothing when the response carries no board file', () => {
    expect(pcbText({})).toBe('')
    expect(pcbText(null)).toBe('')
    expect(pcbText({ kicad_pcb: null })).toBe('')
  })

  it('returns nothing for a board file that is not a string', () => {
    expect(pcbText({ kicad_pcb: { version: 20240108 } })).toBe('')
    expect(pcbText({ kicad_pcb: 42 })).toBe('')
  })

  it('treats a whitespace-only board file as absent, so no empty file is offered', () => {
    expect(pcbText({ kicad_pcb: '   \n ' })).toBe('')
  })
})

describe('downloadText', () => {
  // The rest of downloadText is Blob and anchor work the node environment has
  // no business running; the guard is the part with a decision in it, and it is
  // the reason a save with nothing to save never reaches `document` at all.
  let createObjectURL

  beforeEach(() => {
    createObjectURL = vi.spyOn(URL, 'createObjectURL')
    clearLog()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('saves nothing, and touches no browser API, when there is no text', () => {
    expect(downloadText('', 'silkscreen.txt')).toBe(false)
    expect(downloadText(undefined, 'silkscreen.txt')).toBe(false)
    expect(createObjectURL).not.toHaveBeenCalled()
  })

  it('records nothing when there is no text, so an unsaved file leaves no trace', async () => {
    downloadText('', 'silkscreen.txt')
    await Promise.resolve()
    expect(get(log).entries).toEqual([])
  })

  it('defaults to the log export type, the one callers share', () => {
    expect(LOG_TEXT_MIME).toBe('text/plain;charset=utf-8')
  })
})

describe('downloadPcb', () => {
  it('saves nothing when there is no text', () => {
    expect(downloadPcb('')).toBe(false)
    expect(downloadPcb(pcbText({}))).toBe(false)
  })

  it('names the file after the board, and sends it as bytes', () => {
    expect(PCB_FILENAME).toBe('board.kicad_pcb')
    expect(PCB_MIME).toBe('application/octet-stream')
  })
})
