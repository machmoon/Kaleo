import { describe, expect, it, vi } from 'vitest'

import { saveTextWithDialog } from './desktop-files.js'

describe('native desktop saves', () => {
  it('writes exactly the selected path and text', async () => {
    const choosePath = vi.fn().mockResolvedValue('/Users/ada/board.kicad_pcb')
    const writeText = vi.fn().mockResolvedValue(undefined)

    const saved = await saveTextWithDialog('(kicad_pcb)', 'board.kicad_pcb', {
      choosePath,
      writeText,
    })

    expect(saved).toBe('/Users/ada/board.kicad_pcb')
    expect(choosePath).toHaveBeenCalledWith({ defaultPath: 'board.kicad_pcb' })
    expect(writeText).toHaveBeenCalledWith('/Users/ada/board.kicad_pcb', '(kicad_pcb)')
  })

  it('does not write when the user cancels the dialog', async () => {
    const writeText = vi.fn()

    const saved = await saveTextWithDialog('trace', 'trace.txt', {
      choosePath: vi.fn().mockResolvedValue(null),
      writeText,
    })

    expect(saved).toBe('')
    expect(writeText).not.toHaveBeenCalled()
  })
})
