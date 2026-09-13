// Public datasheets a user can opt into without hunting for a URL during a
// demo. A preset is only copied into the form after its button is clicked.
//
// A preset must resolve to actual PDF bytes, not to a page about a PDF. The
// distributor link this list used to carry (datasheet.lcsc.com/...C6186.pdf)
// now redirects to an LCSC product page that answers 200 text/html, so the
// service downloaded 126KB of markup and the run died at the model. Prefer the
// manufacturer's own copy, and check `content-type: application/pdf` before
// adding one here.
//
// http, not https, because advanced-monolithic.com fails the TLS handshake.
// The engine allows both schemes and validates the URL against SSRF on every
// redirect hop, and the PDF is a public document with nothing to protect in
// transit.

export const DATASHEET_PRESETS = Object.freeze([
  Object.freeze({
    part: 'AMS1117-3.3',
    manufacturer: 'Advanced Monolithic Systems',
    url: 'http://www.advanced-monolithic.com/pdf/ds1117.pdf',
  }),
])
