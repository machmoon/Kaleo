import { describe, expect, it } from 'vitest'

import { normalizeRequest } from './api.js'
import { DATASHEET_PRESETS } from './datasheets.js'

describe('datasheet presets', () => {
  it('includes the public AMS1117-3.3 PDF', () => {
    expect(DATASHEET_PRESETS).toContainEqual({
      part: 'AMS1117-3.3',
      manufacturer: 'Advanced Monolithic Systems',
      url: 'http://www.advanced-monolithic.com/pdf/ds1117.pdf',
    })
  })

  it('points every preset at a PDF the service can download', () => {
    // The previous AMS1117 link was a distributor page that answered
    // 200 text/html from a .pdf URL, which the run could only discover at the
    // model. A preset is demo material, so it has to be a direct document.
    for (const preset of DATASHEET_PRESETS) {
      expect(preset.url).toMatch(/^https?:\/\//)
      expect(preset.url.toLowerCase()).toMatch(/\.pdf$/)
      expect(preset.url).not.toMatch(/lcsc\.com/)
    }
  })

  it('can be copied into a ready-to-submit request', () => {
    const preset = DATASHEET_PRESETS[0]
    const request = normalizeRequest({
      intent: 'a 3.3 V LDO regulator board',
      datasheets: { [preset.part]: preset.url },
      time_limit_s: 20,
      review: true,
    })

    expect(request.datasheets).toEqual({ [preset.part]: preset.url })
  })
})
