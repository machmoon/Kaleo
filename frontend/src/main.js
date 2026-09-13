// First line on purpose: imports are hoisted and evaluated in order, so the
// console and window hooks have to install before anything else can log.
import './lib/capture.js'

import { readStored as readSkin, skinAttribute } from './lib/skin.js'
import { readStored, themeAttribute } from './lib/theme.js'

import { mount } from 'svelte'

// Self-hosted so the demo survives bad wifi — no Google Fonts request.
import '@fontsource/chivo/300.css'
import '@fontsource/chivo/400.css'
import '@fontsource/chivo/500.css'
import '@fontsource/chivo/600.css'
import '@fontsource/chivo/700.css'
import '@fontsource/chivo-mono/400.css'
import '@fontsource/chivo-mono/500.css'
import '@fontsource/chivo-mono/600.css'

import './styles/tokens.css'
// After tokens.css on purpose: the skin is an override block, and a
// same-specificity override has to come second to win.
import './styles/glass.css'
import './styles/base.css'
import App from './App.svelte'

// The one write of the theme attribute at startup, ahead of the mount so the
// first frame is already the chosen palette. No stored choice leaves the
// attribute off, which is what keeps tokens.css following the OS.
const theme = themeAttribute(readStored(globalThis.localStorage))
if (theme) document.documentElement.dataset.theme = theme

// Same one-write rule for the material. The default skin writes nothing,
// which is what leaves the plain Drafting Table tokens in force.
const skin = skinAttribute(readSkin(globalThis.localStorage))
if (skin) document.documentElement.dataset.skin = skin

export default mount(App, { target: document.getElementById('app') })
