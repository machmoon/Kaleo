// The global "focus the prompt" shortcut (cmd+shift+i in shortcuts.rs) ends
// in a `focus-text-input` event to the webview. The overlay is a
// non-activating panel over KiCad, so the one click it would otherwise take
// to reach the field is the click that leaves KiCad — the whole cost the
// shortcut exists to avoid. This is the last step of that shortcut.

/**
 * Focus the prompt field mounted under `root`, and say whether one was.
 * Nothing is focused when the bar is still folded to the pill; the caller
 * expands it first and asks again once it has rendered.
 */
export function focusPromptIn(root: ParentNode | null | undefined): boolean {
  const input = root?.querySelector<HTMLInputElement>("input:not([disabled])");
  if (!input) return false;
  input.focus();
  return document.activeElement === input;
}
