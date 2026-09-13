// Getting text out of the browser: the board file the service returns, and the
// debug console's log exports.

import { saveTextWithDialog } from './desktop-files.js'
import { LOG_TEXT_MIME, logError, logEvent } from './log.js'

export const PCB_FILENAME = 'board.kicad_pcb'
export const PCB_MIME = 'application/octet-stream'

/** The .kicad_pcb text on a response, or '' when it carries none. Callers gate
    the download control on this, so a run without a board offers no button. */
export function pcbText(result) {
  const text = result && typeof result.kicad_pcb === 'string' ? result.kicad_pcb : ''
  return text.trim() ? text : ''
}

/** Writes the text to a Blob and saves it. Returns whether anything was saved. */
export function downloadText(text, filename, type = LOG_TEXT_MIME) {
  if (!text) return false
  if (globalThis.__SILKSCREEN_BASE__) {
    return saveTextWithDialog(text, filename)
      .then((path) => {
        if (!path) return false
        logEvent('ui.download', `saved ${filename}`, {
          filename,
          path,
          type,
          chars: text.length,
          native: true,
        })
        return true
      })
      .catch((error) => {
        logError('ui.download', `could not save ${filename}`, {
          filename,
          type,
          message: String(error?.message ?? error),
          native: true,
        })
        return false
      })
  }
  const url = URL.createObjectURL(new Blob([text], { type }))
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  // Revoked a tick later rather than inline: Firefox and Safari read a URL
  // revoked in the same task as the click as an aborted download.
  setTimeout(() => URL.revokeObjectURL(url), 0)
  logEvent('ui.download', `saved ${filename}`, { filename, type, chars: text.length })
  return true
}

export function downloadPcb(text, filename = PCB_FILENAME) {
  return downloadText(text, filename, PCB_MIME)
}
