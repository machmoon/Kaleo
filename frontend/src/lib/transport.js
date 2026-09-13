const LOOPBACK_HOST = '127.0.0.1'

function sidecarOrigin(baseUrl) {
  let parsed
  try {
    parsed = new URL(baseUrl)
  } catch {
    throw new TypeError('The desktop sidecar URL must be a canonical loopback origin.')
  }

  const canonical = parsed.origin
  const isOrigin = baseUrl === canonical || baseUrl === `${canonical}/`
  if (
    parsed.protocol !== 'http:' ||
    parsed.hostname !== LOOPBACK_HOST ||
    !parsed.port ||
    !isOrigin
  ) {
    throw new TypeError('The desktop sidecar URL must be a canonical loopback origin.')
  }
  return canonical
}

/** Resolve an API path without changing same-origin browser requests. */
export function resolveApiUrl(path, baseUrl = '') {
  if (!baseUrl) return path

  const origin = sidecarOrigin(baseUrl)
  if (typeof path !== 'string' || !path.startsWith('/') || path.startsWith('//')) {
    throw new TypeError('The desktop API path must be relative to the loopback sidecar.')
  }
  return `${origin}${path}`
}

/** Select browser fetch for the web app and Tauri fetch for the desktop shell. */
export function createTransport({ baseUrl = '', webFetch, nativeFetch }) {
  const desktop = Boolean(baseUrl)
  if (desktop) sidecarOrigin(baseUrl)

  return (path, init) => {
    const implementation = desktop ? nativeFetch : webFetch
    if (typeof implementation !== 'function') {
      throw new TypeError(`No ${desktop ? 'native' : 'browser'} fetch implementation is available.`)
    }
    return implementation(resolveApiUrl(path, baseUrl), init)
  }
}
