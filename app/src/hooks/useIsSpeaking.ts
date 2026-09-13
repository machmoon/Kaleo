// "Am I talking?", as a React value.
//
// The strip needs this and cannot get it from the listener: while I speak the
// wake ear is deliberately ducked (see the duck in the overlay page), so
// `wake.listening` goes false and anything keyed off it — the listening panel,
// the orb — would tear down and rebuild for the length of every spoken reply.
// Speaking is its own fact and is published as one.

import { useEffect, useState } from "react";
import { isAnnouncing, subscribeSpeaking } from "@/lib/speech";

export function useIsSpeaking(): boolean {
  const [speaking, setSpeaking] = useState(isAnnouncing);
  useEffect(() => {
    // Re-read on mount: an utterance may have started between the initial
    // state and the subscription.
    setSpeaking(isAnnouncing());
    return subscribeSpeaking(setSpeaking);
  }, []);
  return speaking;
}
