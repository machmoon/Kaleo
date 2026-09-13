import { useEffect, useRef, useState } from "react";

/**
 * Whether the engineer has looked away from the overlay since the newest
 * artifact landed in KiCad.
 *
 * The product's whole claim is that the review happens in KiCad rather than in
 * the app. Nothing anywhere checked it: every approval control assumed the
 * looking had happened. This is the honest approximation a webview can make.
 * The overlay can observe its own focus, so it knows when you left, never
 * where you went. Leaving the overlay after a stage pushed something into
 * KiCad is read as having gone to look at it.
 *
 * It is deliberately a nag and never a block. A false negative costs one
 * ghosted button that still works; a block would strand anyone whose window
 * manager, screen reader or second monitor breaks the assumption.
 *
 * `marker` identifies the newest artifact. Pass null when there is nothing to
 * review, which always reads as reviewed.
 */
export function useReviewedInKicad(marker: string | null): boolean {
  const [seen, setSeen] = useState<string | null>(null);
  const markerRef = useRef(marker);
  markerRef.current = marker;

  useEffect(() => {
    const away = () => {
      // Record the artifact that was current at the moment of leaving. A
      // stage that lands while the overlay is in the background is not
      // reviewed by the fact that the engineer is already elsewhere.
      if (markerRef.current !== null) setSeen(markerRef.current);
    };
    window.addEventListener("blur", away);
    return () => window.removeEventListener("blur", away);
  }, []);

  return marker === null || seen === marker;
}
