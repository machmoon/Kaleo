// The living dot: one indicator for what the overlay is doing with the room.
//
// It replaces the static engine dot at the head of the strip, because the
// founder asked for exactly that — "i think it can be the green dot that
// changes and then the mic on and off is always there". The dot says what is
// happening; the microphone button beside it stays put and stays the switch.
//
// The rule that outranks every aesthetic decision here:
//
//   **It must never animate as though listening when the microphone is not
//   open.**
//
// That lie is the bug this whole thread came from: the old indicator was
// driven by `wake.enabled`, the *switch*, so it went green the instant
// someone clicked — while the mic was still opening, while a permission probe
// was in flight, and even after the listener had refused to arm. `OrbInput`
// therefore has no `enabled` field at all. It is structurally impossible to
// draw "listening" from the switch, because the switch is not an input to
// this component: `micOpen` comes from `wake.listening`, which is set only by
// the listener's own `onState("listening", …)` — the microphone, not the
// intent to open it.
//
// The second rule: colour is never the only carrier. Every state differs in
// *shape* or in *motion* as well as in hue — a hollow core, a broken ring, a
// slash, a rotation — so the states survive a monochrome screenshot and a
// colour-blind reader.
//
// The third: with no amplitude data the orb degrades to a plain state rather
// than inventing motion. `useMicLevel` reports `hasSignal: false` when nobody
// is measuring, and "nobody is measuring" is drawn differently from "the room
// is quiet" (see `data-signal`).

import { useEffect, useState } from "react";
import type { EngineHealth } from "@/hooks";
import { useMicLevel, usePushToTalk } from "@/hooks/useMicLevel";
import { speaker } from "@/lib/speech";

/** What the orb is showing. `idle` covers "muted, and nothing happening". */
export type OrbState =
  | "fault"
  | "unknown"
  | "idle"
  | "listening"
  | "heard"
  | "recording"
  | "thinking"
  | "speaking";

/** The engine's own state, kept beside the voice state rather than folded in. */
export type OrbHealth = "up" | "down" | "unknown";

/**
 * Everything the orb is allowed to know.
 *
 * Note what is absent: the wake-word *switch*. See the file header.
 */
export interface OrbInput {
  /** The engine answered its last probe. */
  engineOk: boolean;
  /** A probe has landed at all; false means "we do not know yet". */
  engineKnown: boolean;
  /** The microphone is open and a detector is running (`wake.listening`). */
  micOpen: boolean;
  /** Push-to-talk is recording right now. */
  recording: boolean;
  /** The wake word landed within the last few seconds (`wake.justHeard`). */
  justHeard: boolean;
  /** A run or a transcription is in flight. */
  thinking: boolean;
  /** TTS is talking back. */
  speaking: boolean;
}

/**
 * The one state the orb draws, in precedence order.
 *
 * The order is an argument, not a preference:
 *
 *  1. `recording` — the engineer is holding the button. Nothing outranks the
 *     thing their finger is doing.
 *  2. `heard` — the wake word just landed; that is the moment they are
 *     looking for and it lasts seconds.
 *  3. `listening` — a hot microphone is the one fact this app must never
 *     hide, so it outranks a run in flight and it outranks the voice talking.
 *  4. `speaking`, then `thinking` — work the machine is doing, not the room.
 *  5. Otherwise the engine's own state, because with nothing else to say the
 *     orb goes back to being the dot it replaced.
 */
export function orbState(input: OrbInput): OrbState {
  if (input.recording) return "recording";
  if (input.justHeard) return "heard";
  if (input.micOpen) return "listening";
  if (input.speaking) return "speaking";
  if (input.thinking) return "thinking";
  if (!input.engineKnown) return "unknown";
  return input.engineOk ? "idle" : "fault";
}

/**
 * The engine's state, always, whatever the voice is doing.
 *
 * The orb took the engine dot's job, so engine health cannot be something it
 * only shows when it has nothing better to draw. `orbState` may be
 * `listening` while the engine is down; the broken outer ring says so at the
 * same time, and the title says it in words.
 */
export function orbHealth(input: Pick<OrbInput, "engineOk" | "engineKnown">): OrbHealth {
  if (!input.engineKnown) return "unknown";
  return input.engineOk ? "up" : "down";
}

/** Does this state mean the microphone is open? Used for the amplitude ring. */
export function orbIsHot(state: OrbState): boolean {
  return state === "listening" || state === "recording" || state === "heard";
}

/**
 * What the orb says in words, in the first person, engine first.
 *
 * Pure and exported so the sentences are pinned by test: this is the only
 * explanation anyone gets for a coloured shape, and a state that quietly
 * reuses another state's sentence is the bug.
 */
export function orbTitle(
  state: OrbState,
  health: OrbHealth,
  baseUrl: string,
  detail?: string | null
): string {
  const engine =
    health === "unknown"
      ? `Checking ${baseUrl}…`
      : health === "up"
        ? `Engine up at ${baseUrl}.`
        : `Engine unreachable at ${baseUrl}${detail ? ` — ${detail}` : ""}.`;
  const voice = (() => {
    switch (state) {
      case "recording":
        return "I am recording you — hold the microphone and talk.";
      case "heard":
        return "I heard my name.";
      case "listening":
        return "The microphone is open and I am listening for my name.";
      case "speaking":
        return "I am talking back.";
      case "thinking":
        return "I am working on it.";
      case "fault":
        return "I am not listening, and I could not reach the engine.";
      case "unknown":
        return "I am not listening.";
      default:
        return "I am not listening — the microphone is closed.";
    }
  })();
  return `${engine} ${voice} Click to re-check the engine.`;
}

/** True when the OS asks for less motion. Falls back to "no" where unaskable. */
function prefersReducedMotion(): boolean {
  const mm = (globalThis as { matchMedia?: (q: string) => MediaQueryList }).matchMedia;
  if (typeof mm !== "function") return false;
  try {
    return mm.call(globalThis, "(prefers-reduced-motion: reduce)").matches === true;
  } catch {
    return false;
  }
}

const ORB_CSS = `
.kv-orb { display:block; overflow:visible; }
.kv-orb * { transform-box: fill-box; transform-origin: center; }
.kv-orb .kv-core { transition: r 120ms linear, opacity 120ms linear; }
.kv-orb .kv-amp { transition: r 60ms linear, opacity 60ms linear; }

@keyframes kv-breathe { 0%,100% { opacity:.45; transform:scale(.86); }
                        50%     { opacity:.95; transform:scale(1.08); } }
@keyframes kv-halo    { 0%   { opacity:.55; transform:scale(.72); }
                        70%  { opacity:0;   transform:scale(1.35); }
                        100% { opacity:0;   transform:scale(1.35); } }
@keyframes kv-spin    { from { transform:rotate(0deg); } to { transform:rotate(360deg); } }
@keyframes kv-flash   { 0% { opacity:1; transform:scale(.5); }
                        100% { opacity:0; transform:scale(1.6); } }

/* The idle state gets NO animation, deliberately, and that is a considered
   departure from the brief's "breathes slowly when idle".

   Idle here means the microphone is closed. A green core pulsing at 4.2s
   next to one pulsing at 1.9s asks the reader to tell two rates apart with
   no second orb beside it for comparison — which in practice means a
   breathing green dot at a shut microphone, one glance away from the exact
   lie this lane exists to kill. Still is unambiguous, and it also makes
   every hot state announce itself the instant motion starts. (Raised as a P1
   by the QA lane on the first render; agreed, and stillness wins.) */
.kv-orb[data-motion="animated"][data-state="listening"] .kv-core { animation: kv-breathe 1.9s ease-in-out infinite; }
.kv-orb[data-motion="animated"][data-state="recording"] .kv-core { animation: kv-breathe 1.1s ease-in-out infinite; }
.kv-orb[data-motion="animated"] .kv-halo { animation: kv-halo 2.1s ease-out infinite; }
.kv-orb[data-motion="animated"] .kv-halo-2 { animation-delay: 1.05s; }
.kv-orb[data-motion="animated"] .kv-arc { animation: kv-spin 1.15s linear infinite; }
.kv-orb[data-motion="animated"] .kv-flash { animation: kv-flash .95s ease-out infinite; }
.kv-orb[data-motion="still"] .kv-halo,
.kv-orb[data-motion="still"] .kv-flash { opacity:.5; }
`;

export interface VoiceOrbProps {
  engine: EngineHealth;
  baseUrl: string;
  /** `wake.listening` — the microphone. Never `wake.enabled`. */
  micOpen?: boolean;
  /** `wake.justHeard`. */
  justHeard?: boolean;
  /**
   * Push-to-talk is recording. Omit it and the orb asks the control that
   * actually holds the recorder (`setPushToTalk`), because the caller here
   * cannot see it: holding the mic button stops the wake listener, so
   * `micOpen` is false for the whole hold.
   */
  recording?: boolean;
  /** A run or a transcription is in flight. */
  thinking?: boolean;
  /** Test seams. */
  isSpeaking?: () => boolean;
  reducedMotion?: boolean;
}

/**
 * The strip's state indicator: an 18 px orb where the engine dot used to be.
 *
 * Layers, outside in: a health ring (drawn only when the engine is not known
 * to be up — solid-with-a-gap for down, dotted for unknown), an amplitude
 * ring that follows the real microphone, state motion (haloes for speech, a
 * rotating arc for work, a flash for a wake), and a core whose size and shape
 * carry the state at any colour.
 */
export const VoiceOrb = ({
  engine,
  baseUrl,
  micOpen = false,
  justHeard = false,
  recording,
  thinking = false,
  isSpeaking,
  reducedMotion,
}: VoiceOrbProps) => {
  const [speaking, setSpeaking] = useState(false);
  const held = usePushToTalk();
  const still = reducedMotion ?? prefersReducedMotion();

  // The speaker has no event, so it is polled — slowly, and only written to
  // state when the answer changes, so an idle overlay does not re-render.
  useEffect(() => {
    const read = isSpeaking ?? (() => speaker.isSpeaking());
    const tick = () => setSpeaking((prev) => (prev === read() ? prev : read()));
    tick();
    const timer = setInterval(tick, 250);
    return () => clearInterval(timer);
  }, [isSpeaking]);

  const input: OrbInput = {
    engineOk: engine.ok,
    engineKnown: engine.lastCheckedAt !== null,
    micOpen,
    recording: recording ?? held,
    justHeard,
    thinking,
    speaking,
  };
  const state = orbState(input);
  const health = orbHealth(input);
  const hot = orbIsHot(state);

  // The amplitude subscription exists only while the microphone is open. A
  // meter running against a closed mic is how an indicator ends up waving at
  // nothing.
  const { level, hasSignal } = useMicLevel(hot, { still });

  const title = orbTitle(state, health, baseUrl, engine.detail);
  // `idle` is deliberately NOT `--strip-ok` any more. A green dot sitting at
  // the head of the strip all day is a claim being made continuously about a
  // system that is fine, and it was the first thing the eye landed on; the
  // user asked for it to go. What replaced it is silence — the resting orb is
  // the strip's own foreground, the same as any other quiet glyph.
  //
  // Nothing became invisible. The states that *are* worth a colour keep one:
  // `fault` and `recording` stay red, `unknown` stays grey, the health ring
  // below still draws for down and unknown and draws nothing for up, and an
  // engine that is not answering also says so on the ⏎ control's own label
  // (`submitReason`) and in the engine-down banner in the card.
  //
  // `listening` and `heard` keep `--strip-ok`, because those are transient:
  // an open microphone is a claim that expires, and it is the one state where
  // a colour is telling the room something it cannot otherwise see.
  const tone =
    state === "recording" || state === "fault"
      ? "var(--strip-bad)"
      : state === "unknown"
        ? "var(--strip-fg-3)"
        : state === "thinking" || state === "speaking"
          ? "var(--strip-fg)"
          : state === "idle"
            ? "var(--strip-fg-2)"
            : "var(--strip-ok)";

  const coreR = state === "heard" ? 3.6 : state === "idle" || state === "unknown" ? 2.6 : 3.1;
  const ampR = 5.2 + level * 4.6;

  return (
    <button
      type="button"
      className="flex size-[18px] shrink-0 items-center justify-center"
      title={title}
      aria-label={title}
      onClick={engine.recheck}
      // Kept from the dot this replaces: the same id, the same click, so the
      // strip's existing tests and habits still find the engine here.
      data-testid="engine-status"
      data-online={health === "unknown" ? "unknown" : String(engine.ok)}
    >
      <svg
        viewBox="0 0 24 24"
        width="18"
        height="18"
        className="kv-orb"
        aria-hidden="true"
        data-testid="voice-orb"
        data-state={state}
        data-health={health}
        // "live" means something is measuring the microphone; "none" means
        // nothing is, which is a different fact from a quiet room.
        data-signal={hot ? (hasSignal ? "live" : "none") : "off"}
        data-level={hot && hasSignal ? level.toFixed(2) : undefined}
        data-motion={still ? "still" : "animated"}
        style={{ color: tone }}
      >
        <style>{ORB_CSS}</style>

        {/* Engine health, always, whatever the voice is doing. A ring with a
            gap in it is "down" without relying on red; a dotted ring is
            "not known yet". An engine that is up draws nothing, because the
            absence of a warning is the quietest way to say so. */}
        {health !== "up" ? (
          <circle
            className="kv-health"
            cx="12"
            cy="12"
            r="10"
            fill="none"
            stroke={health === "down" ? "var(--strip-bad)" : "var(--strip-fg-3)"}
            strokeWidth="1.6"
            strokeLinecap="round"
            strokeDasharray={health === "down" ? "34 29" : "1.5 3.5"}
            data-testid="orb-health-ring"
          />
        ) : null}

        {/* The microphone, drawn from the microphone. Only ever rendered
            while the mic is open AND something is actually measuring it. */}
        {hot && hasSignal ? (
          <circle
            className="kv-amp"
            cx="12"
            cy="12"
            r={ampR}
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            opacity={0.25 + level * 0.6}
            data-testid="orb-amplitude"
          />
        ) : null}

        {/* Talking back: two haloes leaving the centre. */}
        {state === "speaking" ? (
          <>
            <circle className="kv-halo" cx="12" cy="12" r="7" fill="none" stroke="currentColor" strokeWidth="1.5" />
            <circle className="kv-halo kv-halo-2" cx="12" cy="12" r="7" fill="none" stroke="currentColor" strokeWidth="1.5" />
          </>
        ) : null}

        {/* Working: an arc going round, which is a different motion from
            anything the microphone does. */}
        {state === "thinking" ? (
          <circle
            className="kv-arc"
            cx="12"
            cy="12"
            r="7"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.7"
            strokeLinecap="round"
            strokeDasharray="12 32"
            data-testid="orb-arc"
          />
        ) : null}

        {/* The wake landed: one expanding flash, plus a bigger core. */}
        {state === "heard" ? (
          <circle className="kv-flash" cx="12" cy="12" r="7" fill="none" stroke="currentColor" strokeWidth="1.8" />
        ) : null}

        <circle
          className="kv-core"
          cx="12"
          cy="12"
          r={coreR}
          fill={state === "unknown" ? "none" : "currentColor"}
          stroke="currentColor"
          strokeWidth={state === "unknown" ? 1.5 : 0}
          data-testid="orb-core"
        />

        {/* Engine down and nothing else to say: a slash through the core, so
            the fault reads without colour. */}
        {state === "fault" ? (
          <line
            x1="7.5"
            y1="16.5"
            x2="16.5"
            y2="7.5"
            stroke="var(--strip-bad)"
            strokeWidth="1.8"
            strokeLinecap="round"
            data-testid="orb-fault-slash"
          />
        ) : null}
      </svg>
    </button>
  );
};
