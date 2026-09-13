// The one gate in front of the mouth.
//
// `speaker` decides that only one thing is said at a time. This decides
// whether anything is said at all, and it exists because a voice that talks
// over its own microphone is not a conversation, it is a feedback loop:
//
//  - **Muted wins.** Every spoken line in the app goes through here, so the
//    one toggle in the voice menu silences all of them. A path that called
//    `speaker.speak` directly would keep talking after the mute — the desk
//    caption did exactly that until this module existed.
//  - **The microphone has right of way.** While push-to-talk is held the
//    floor is taken, an utterance in progress is cut, and a new one yields
//    rather than queueing: one room, one microphone, one speaker, and a
//    stale sentence talked into a live recording ends up in the transcript.
//  - **The wake ear is ducked, not deafened.** The `windows` ear pays for
//    four seconds of room audio per call, and audio played from this
//    machine's own speakers reaches its own microphone — so an unducked
//    reply is both a paid window of me listening to me and a real chance of
//    hearing my own name back. The page registers a duck that stops the ear
//    while I talk and starts it again after; the same trick push-to-talk
//    already uses, for the same reason.
//  - **Silence is honest.** An unavailable `speechSynthesis` returns
//    `"unavailable"` rather than a resolved promise that looks like speech.

import { webSpeechAvailable } from "./backends";
import {
  chooseBackendName,
  speaker as sharedSpeaker,
  voiceStatus,
  type Speaker,
} from "./speaker";
import { isVoiceEnabled, loadVoiceSettings, type VoiceSettings } from "./settings";
import { speakable } from "./speakable";

/** Why a line was or was not said. Returned rather than logged so tests can pin it. */
export type AnnounceOutcome =
  | "spoken"
  | "empty"
  | "muted"
  | "yielded"
  | "unavailable";

/** Can anything actually make sound in this webview right now? */
export function voiceAvailable(settings: VoiceSettings = loadVoiceSettings()): boolean {
  // A key means the audio comes back as mp3 bytes and plays through an
  // Audio element, which needs no speechSynthesis at all.
  if (chooseBackendName(settings.elevenLabsKey) === "elevenlabs") return true;
  // The engine's own voice plays PCM through Web Audio and likewise needs no
  // speechSynthesis. Asking `webSpeechAvailable()` here was the same mistake
  // `speaker.engineBaseUrl` made: it treated the platform voice as the only
  // voice, so a webview without `speechSynthesis` reported "no voice" while
  // Kokoro was sitting there ready to answer.
  const status = voiceStatus(settings);
  if (status.backend === "service") return true;
  if (status.backend === "webspeech") return webSpeechAvailable();
  return false;
}

// ------------------------------------------------------------------- floor

let micHolders = 0;

/** Is the microphone holding the floor — i.e. is the human talking? */
export function micHasFloor(): boolean {
  return micHolders > 0;
}

/**
 * Take the floor for the microphone, cutting anything being said.
 *
 * Returns the release, which is idempotent: a pointer-up that fires twice
 * (up, then leave) must not drop the count below the holders that remain.
 */
export function takeMicFloor(speaker: Speaker = sharedSpeaker): () => void {
  micHolders += 1;
  // Not "let me finish this sentence": the recording has already started.
  speaker.stop();
  let released = false;
  return () => {
    if (released) return;
    released = true;
    micHolders = Math.max(0, micHolders - 1);
  };
}

/** Test seam only: forget any floor a failed test left held. */
export function resetMicFloor(): void {
  micHolders = 0;
}

// -------------------------------------------------------------------- duck

/** Pause whatever is listening; the returned function starts it again. */
export type SpeechDuck = () => (() => void) | void;

let duck: SpeechDuck | null = null;

/**
 * Register the app's one duck. The overlay page owns it, because the wake
 * listener is the page's; `null` unregisters on unmount.
 */
export function setSpeechDuck(next: SpeechDuck | null): void {
  duck = next;
}

// ---------------------------------------------------------------- announce

// --------------------------------------------------------------- speaking

let speaking = 0;
const speakingListeners = new Set<(speaking: boolean) => void>();

/** Am I saying something right now? */
export function isAnnouncing(): boolean {
  return speaking > 0;
}

/**
 * Subscribe to that, because the strip needs it.
 *
 * `speaker.isSpeaking()` is a poll, and the bar cannot poll: while I talk the
 * wake ear is ducked, which makes `wake.listening` false, which would tear
 * the listening panel down and flash the text field back into the strip for
 * the length of every spoken reply. A bar that knows I am speaking can hold
 * its shape through the whole exchange instead.
 */
export function subscribeSpeaking(listener: (speaking: boolean) => void): () => void {
  speakingListeners.add(listener);
  return () => speakingListeners.delete(listener);
}

function setSpeaking(next: number): void {
  const was = speaking > 0;
  speaking = Math.max(0, next);
  const now = speaking > 0;
  if (was === now) return;
  for (const listener of speakingListeners) {
    try {
      listener(now);
    } catch {
      // A subscriber that throws is not allowed to abort an utterance.
    }
  }
}

export interface AnnounceDeps {
  speaker?: Speaker;
  enabled?: () => boolean;
  available?: () => boolean;
}

/**
 * Say one line, if this is a moment where saying it is allowed.
 *
 * Never throws — `speaker.speak` already degrades every failure to silence
 * plus one warning — and never queues: a line that arrives while the human
 * is talking is dropped, because by the time the floor is free it is about
 * something that already happened.
 */

export async function announce(
  text: string,
  deps: AnnounceDeps = {}
): Promise<AnnounceOutcome> {
  const trimmed = text.trim();
  if (!trimmed) { return "empty"; }
  const enabled = deps.enabled ?? isVoiceEnabled;
  if (!enabled()) { return "muted"; }
  if (micHasFloor()) { return "yielded"; }
  const available = deps.available ?? (() => voiceAvailable());
  if (!available()) { return "unavailable"; }

  const speaker = deps.speaker ?? sharedSpeaker;
  let resume: (() => void) | void;
  try {
    resume = duck?.();
  } catch {
    // A listener that cannot be paused is not a reason to go mute.
    resume = undefined;
  }
  setSpeaking(speaking + 1);
  try {
    // The display/speech split, applied at the one place it cannot be
    // bypassed: `trimmed` is what the strip shows, `speakable(trimmed)` is
    // what the mouth gets. Doing this in `moments`/`summarize` instead would
    // have meant remembering it at every composer, and the desk caption —
    // which is model text, not composed here at all — could not have been
    // covered by any of them.
    const spk = speakable(trimmed);
    await speaker.speak(spk);
  } finally {
    setSpeaking(speaking - 1);
    // If the human took the floor while I was talking, whoever took it owns
    // the microphone now and will restart the ear themselves; restarting it
    // here would arm it underneath a live recording.
    if (!micHasFloor()) {
      try {
        resume?.();
      } catch {
        /* the ear failing to re-arm is the ear's to report, not the voice's */
      }
    }
  }
  return "spoken";
}
