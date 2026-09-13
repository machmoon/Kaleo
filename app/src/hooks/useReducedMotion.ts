import { useEffect, useState } from "react";

const QUERY = "(prefers-reduced-motion: reduce)";

/** The same probe `VoiceOrb` keeps privately; false wherever it cannot ask. */
function prefersReducedMotion(): boolean {
  const mm = (globalThis as { matchMedia?: (q: string) => MediaQueryList }).matchMedia;
  if (typeof mm !== "function") return false;
  try {
    return mm.call(globalThis, QUERY).matches === true;
  } catch {
    return false;
  }
}

/**
 * Whether the OS asked for less motion, kept live.
 *
 * Consumers put `data-motion="still"` on their root when this is true —
 * `motion.css` then disables the `kv-*` vocabulary under that ancestor. The
 * media query alone would do the same for CSS, but a component that runs a
 * `setTimeout` crossfade (the wizard's step swap) needs the answer in JS too,
 * or it keeps the outgoing step mounted for a fade that never plays.
 */
export function useReducedMotion(): boolean {
  const [still, setStill] = useState<boolean>(prefersReducedMotion);
  useEffect(() => {
    const mm = (globalThis as { matchMedia?: (q: string) => MediaQueryList }).matchMedia;
    if (typeof mm !== "function") return;
    let list: MediaQueryList;
    try {
      list = mm.call(globalThis, QUERY);
    } catch {
      return;
    }
    const onChange = () => setStill(list.matches === true);
    onChange();
    if (typeof list.addEventListener === "function") {
      list.addEventListener("change", onChange);
      return () => list.removeEventListener("change", onChange);
    }
    return undefined;
  }, []);
  return still;
}

/** The attribute value `motion.css` reads. */
export function motionAttr(still: boolean): "still" | "animated" {
  return still ? "still" : "animated";
}
