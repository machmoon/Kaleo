import { useEffect, useRef, useState } from "react";
import { Input } from "@/components";
import type { EngineHealth } from "@/hooks";
import { VoiceOrb } from "./VoiceOrb";

/**
 * The overlay as an orb: at rest there is an orb and nothing else.
 *
 * The brief was a sentence about a screenshot — "how about the new designs
 * for the bar … i think that is cool bc like its an orb and that could be a
 * diff style" — held against the standing rule that "the overlay isnt meant
 * to have so much visual clutter". The catalogue row is the contract:
 * *"Collapses to a listening orb; the field appears when you speak or
 * click."* So the resting frame has no field, no microphone button, no
 * options popover and no run control. Everything the bar shows in a row,
 * this shows only once it has been asked for.
 *
 * Two rules outrank the aesthetic, and both of them are why this file is
 * mostly composition rather than art:
 *
 * **1. The orb must never claim to be listening when the microphone is not
 * open.** This repo has already shipped that lie once — a green dot driven
 * by `wake.enabled`, the *switch*, which went green while the mic was still
 * opening, while a permission probe was in flight, and even after the
 * listener refused to arm (see `VoiceOrb.tsx`'s header and
 * `WakeWordToggle.tsx`'s "On Mac this is one Gemini clip per click, not
 * always-on like Siri"). A 44 px animated orb is a far louder version of the
 * same claim than an 8 px dot was. So this component does not draw its own
 * state at all: the orb itself is `VoiceOrb`, whose `OrbInput` structurally
 * has no `enabled` field, and every moving thing this file adds on top —
 * the amplitude glow, the pulse — is rendered only under `micOpen`, which is
 * `wake.listening`, the listener's own report that the microphone is open.
 * `listening` (the switch) is accepted as a prop and deliberately reaches
 * nothing that moves; it only changes the resting caption.
 *
 * **2. A control that spends money is visibly paid, and absent — not greyed
 * — when pressing it would not be valid.** So `orb-submit` is not rendered
 * at all unless there is something to run, the engine answered its last
 * probe, and no run is already in flight.
 *
 * On animation: nothing here is ported from LiveKit's aura shader. That file
 * is Polyform Non-Resale 1.0.0, not Apache-2.0, and cannot ship (recorded in
 * `docs/overlay-skins.md`). The two keyframes below are hand-written CSS, and
 * they stop entirely under `prefers-reduced-motion` — an always-on-top
 * overlay that animates forever is a battery and an attention cost, and none
 * of the orbs the research pass opened honours that.
 */

/** How much bigger the skin's orb is than the strip's 18 px indicator. */
const ORB_SCALE = 2.4;

/**
 * True when the OS asks for less motion. Falls back to "no" where unaskable.
 *
 * Duplicated from `VoiceOrb.tsx` rather than imported because that copy is
 * module-private there; the `reducedMotion` prop is the seam tests use, so
 * this function is only the default.
 */
function prefersReducedMotion(): boolean {
  const mm = (globalThis as { matchMedia?: (q: string) => MediaQueryList }).matchMedia;
  if (typeof mm !== "function") return false;
  try {
    return mm.call(globalThis, "(prefers-reduced-motion: reduce)").matches === true;
  } catch {
    return false;
  }
}

/* Written here rather than in a stylesheet for the same reason VoiceOrb does
   it: the skin is one file and the animation is meaningless without the
   element it is on. Both keyframes are attached only to elements that render
   under `micOpen`, so there is no rule that could animate a shut mic. */
const ORB_SKIN_CSS = `
.kv-orbskin-pulse {
  animation: kv-orbskin-pulse 2.4s ease-in-out infinite;
}
@keyframes kv-orbskin-pulse {
  0%, 100% { opacity: .18; transform: scale(1); }
  50%      { opacity: .42; transform: scale(1.14); }
}
/* Belt and braces over the data-motion gate below: even if a future edit
   forgets to check the prop, the OS setting still wins. */
@media (prefers-reduced-motion: reduce) {
  .kv-orbskin-pulse { animation: none; }
}
`;

export interface OrbSkinProps {
  /** Engine health, carried straight to the orb (it owns the engine dot's job). */
  engine: EngineHealth;
  baseUrl: string;
  /**
   * `wake.listening` — the microphone is open and a detector is running.
   * The ONLY input in this file that may make anything move.
   */
  micOpen?: boolean;
  /**
   * `wake.enabled` — the *switch*. Never draws a listening state, never
   * reaches an animation. It exists so the resting caption can distinguish
   * "the ear is off" from "the ear is on and still opening the microphone",
   * which is exactly the window the old green dot lied through.
   */
  listening?: boolean;
  /** `wake.justHeard` — the wake word landed within the last few seconds. */
  justHeard?: boolean;
  /**
   * Microphone amplitude, 0..1. Ignored entirely unless `micOpen`: a meter
   * that keeps waving after the mic shuts is the same lie in a slower form.
   */
  level?: number;
  /** The draft. Owned by the caller, so the text survives the field closing. */
  value: string;
  onChange: (value: string) => void;
  /** Starts a paid run. Only ever reachable from the field's own control. */
  onSubmit: () => void;
  /** A run is in flight. */
  busy?: boolean;
  /** The run state machine's own guard (`canStart`). */
  canStart?: boolean;
  /** The overlay is hidden by the global shortcut; keep focus out of it. */
  hidden?: boolean;
  /**
   * Controlled disclosure. Omit it and the skin owns it: the field opens on
   * a click, on speech, and while there is a draft to see.
   */
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  /** Mute the ear. Rendered only while the microphone is actually open. */
  onStopListening?: () => void;
  /** Test seams, both passed through to the orb. */
  reducedMotion?: boolean;
  isSpeaking?: () => boolean;
}

/**
 * The caption under the orb while the field is closed.
 *
 * Pure and exported so the sentences are pinned by test. The `listening`
 * (switch) line is the one that matters: it says the ear is on and says in
 * the same breath that the microphone is not open yet, because "on" and
 * "listening" being the same word is how the original bug read as true.
 */
export function orbCaption(micOpen: boolean, listening: boolean, busy: boolean): string {
  if (micOpen) return "I’m listening.";
  if (busy) return "Working on it.";
  if (listening) return "Ear on — the microphone isn’t open yet.";
  return "Click to type.";
}

export const OrbSkin = ({
  engine,
  baseUrl,
  micOpen = false,
  listening = false,
  justHeard = false,
  level = 0,
  value,
  onChange,
  onSubmit,
  busy = false,
  canStart = true,
  hidden = false,
  open,
  onOpenChange,
  onStopListening,
  reducedMotion,
  isSpeaking,
}: OrbSkinProps) => {
  const [selfOpen, setSelfOpen] = useState(false);
  const field = useRef<HTMLInputElement | null>(null);
  const still = reducedMotion ?? prefersReducedMotion();

  // Speech opens the field, and so does a draft: text the user already put
  // there must never be hidden behind a collapse they did not ask for.
  const derived = selfOpen || micOpen || justHeard || value.trim() !== "";
  const showField = open ?? derived;

  const setOpen = (next: boolean) => {
    setSelfOpen(next);
    onOpenChange?.(next);
  };

  useEffect(() => {
    if (showField && !hidden) field.current?.focus();
  }, [showField, hidden]);

  // Clamped rather than trusted: `level` crosses a component boundary, and a
  // stray 12 would push the glow off the 600 px window as a click sponge.
  const amp = micOpen ? Math.min(1, Math.max(0, level)) : 0;

  const gate: SubmitInput = {
    value,
    canStart,
    busy,
    hidden,
    engineOk: engine.ok,
    engineKnown: engine.lastCheckedAt !== null,
  };
  const paid = submittable(gate);

  return (
    <div
      className="flex flex-col items-center gap-2 py-1"
      data-testid="orb-skin"
      data-open={showField ? "true" : "false"}
      // Read by tests and by anyone with a screenshot: whether this frame
      // believes the microphone is open, stated as data rather than inferred
      // from a colour.
      data-mic={micOpen ? "open" : "closed"}
      data-motion={still ? "still" : "animated"}
    >
      <style>{ORB_SKIN_CSS}</style>

      {/*
        The orb is `VoiceOrb`, scaled — not a second orb. That component owns
        the one rule this skin must not get wrong, and a copy of it here
        would be a second place for the lie to come back. The inner element
        is a real button (engine re-check, same testid the strip has always
        used); this wrapper takes the same click by bubbling and opens the
        field, so one click both re-checks and opens, and a keyboard user
        reaches it by Tab like they always could.
      */}
      <div
        className="relative flex size-[52px] cursor-text items-center justify-center"
        title="Click to type"
        onClick={() => setOpen(true)}
        data-testid="orb-skin-orb"
      >
        {/* The amplitude glow. Rendered only under `micOpen`, sized only from
            the real level, so there is no code path where it can appear at a
            shut microphone. */}
        {micOpen ? (
          <span
            aria-hidden
            className={`absolute inset-0 rounded-full bg-current opacity-25${
              still ? "" : " kv-orbskin-pulse"
            }`}
            style={{
              color: "var(--strip-ok)",
              transform: `scale(${(0.72 + amp * 0.5).toFixed(3)})`,
            }}
            data-testid="orb-skin-glow"
            data-level={amp.toFixed(2)}
          />
        ) : null}
        <span
          className="relative"
          style={{ transform: `scale(${ORB_SCALE})`, transformOrigin: "center" }}
        >
          <VoiceOrb
            engine={engine}
            baseUrl={baseUrl}
            micOpen={micOpen}
            justHeard={justHeard}
            thinking={busy}
            reducedMotion={still}
            isSpeaking={isSpeaking}
          />
        </span>
      </div>

      {showField ? (
        <div className="flex w-full min-w-0 items-center gap-1.5" data-testid="orb-skin-field">
          <Input
            ref={field}
            placeholder="What do you need built?"
            value={value}
            // Deliberately never `disabled`, matching SpotlightSkin: a
            // disabled input greys itself, drops the caret, and — the part
            // that actually breaks — is skipped by `focusPromptIn`, whose
            // selector is `input:not([disabled])`. So the focus shortcut
            // would stop finding the field exactly while a run is on screen,
            // which is when someone is most likely to reach for it.
            readOnly={hidden}
            className="h-8 min-w-0 flex-1 text-[13px]"
            data-testid="orb-skin-input"
            onChange={(e) => onChange(e.target.value)}
            onKeyDown={(e) => {
              // Escape puts the skin back to what it is meant to be: an orb.
              if (e.key === "Escape") {
                e.preventDefault();
                setOpen(false);
                return;
              }
              if (e.key === "Enter" && !e.shiftKey && paid) {
                e.preventDefault();
                onSubmit();
              }
            }}
          />
          {micOpen && onStopListening ? (
            <button
              type="button"
              className="h-7 shrink-0 rounded-md px-2 text-[11px] text-muted-foreground hover:text-foreground"
              onClick={onStopListening}
              title="Stop listening. Your text is still here."
              data-testid="orb-skin-stop"
            >
              Stop
            </button>
          ) : null}
          {/*
            The paid control, and the only element in this skin that spends
            money. It is ABSENT rather than greyed whenever pressing it would
            not be valid — nothing typed, the engine unreachable or never
            probed, a run already in flight, the overlay hidden. A greyed
            button is an invitation with the reason hidden inside a disabled
            attribute; no button at all is the honest resting frame, and it
            is why the orb sits alone with nothing filled beside it.
          */}
          {paid ? (
            <button
              type="button"
              className="h-7 shrink-0 rounded-md bg-primary px-2.5 text-[11px] font-medium text-primary-foreground"
              onClick={onSubmit}
              title="Generate a board — this calls the model and costs money"
              data-testid="orb-submit"
            >
              Generate
            </button>
          ) : null}
        </div>
      ) : (
        <span
          className="text-[11px] leading-none text-muted-foreground"
          role="status"
          data-testid="orb-skin-caption"
        >
          {orbCaption(micOpen, listening, busy)}
        </span>
      )}
    </div>
  );
};

/** Everything the paid control's existence depends on. */
export interface SubmitInput {
  value: string;
  canStart: boolean;
  busy: boolean;
  hidden: boolean;
  engineOk: boolean;
  /** A probe has landed at all; false means "we do not know yet". */
  engineKnown: boolean;
}

/**
 * May the run control exist in this frame?
 *
 * Pure and exported so the rule is pinned by test on its own, rather than
 * only through the DOM: this is the money gate, and "absent, not greyed"
 * means a false here removes the element, never disables it. The
 * unknown-engine case is deliberate — before the first probe lands nobody
 * knows whether a run would reach anything, and offering to spend on a guess
 * is the third engine state being flattened into two.
 */
export function submittable(input: SubmitInput): boolean {
  if (!input.value.trim()) return false;
  if (input.busy || input.hidden || !input.canStart) return false;
  return input.engineKnown && input.engineOk;
}
