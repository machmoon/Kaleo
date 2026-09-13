import { defineConfig } from 'vitest/config'

// Deliberately separate from vite.config.js: a standalone config wins outright,
// which keeps the Svelte plugin and the dev proxy out of the test run. The four
// lib modules are DOM-free plain JS, so the node environment is the whole need.
export default defineConfig({
  test: {
    environment: 'node',
    include: ['src/**/*.test.js'],
  },
})
