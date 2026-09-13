import { useEffect, useRef, useState } from "react";
import {
  CheckIcon,
  Loader2Icon,
  MicIcon,
  MicOffIcon,
  Volume2Icon,
  VolumeXIcon,
  XIcon,
} from "lucide-react";
import { Button, Popover, PopoverAnchor, PopoverContent } from "@/components";
import type { WakeWord } from "@/hooks/useWakeWord";
import { useIsSpeaking } from "@/hooks/useIsSpeaking";
import { useVoiceInput } from "@/hooks/useVoiceInput";
import { setPushToTalk } from "@/hooks/useMicLevel";
import {
  isVoiceEnabled,
  saveVoiceEnabled,
  speaker,
  takeMicFloor,
} from "@/lib/speech";
import { MAX_RECORDING_MS } from "@/lib/silkscreen/voice";
import { WAKE_WORD } from "@/lib/wake-word";
import { cn } from "@/lib/utils";

const MAX_RECORDING_S = Math.floor(MAX_RECORDING_MS / 1000);

/** How long the button has to be held before it becomes push-to-talk. */
export const HOLD_MS = 400;

export type MicState =
  | "muted"
  | "arming"
  | "unmuted"
  | "heard"
  | "spent"
  | "refused"
  | "recording";

/**
 * The subset of the listener this control reads.
 *
 * `stoppedReason` is not on `WakeWord` yet — it is the patch this lane asked
 * the listener for (scratchpad/reviews/ada-product.md, patch A) — so it is
 * optional and there is a fallback below. A `WakeWord` is assignable either
 * way, and the day the field lands this file needs no change.
 */
export type MicWake = Pick<
  WakeWord,
  "enabled" | "listening" | "justHeard" | "error" | "detail" | "sent" | "cap"
> & { stoppedReason?: "user" | "cap" | "error" | null };

/** Did the listener stop because it spent its budget, rather than being muted? */
function spentBudget(wake: MicWake): boolean {
  if (wake.enabled || wake.error) return false;
  if (wake.stoppedReason != null) return wake.stoppedReason === "cap";
  // Until patch A lands, the listener's own last word is the only witness:
  // a user mute says "stopped listening", the cap says "stopped after N".
  return wake.cap > 0 && wake.sent >= wake.cap && /stopped after/i.test(wake.detail ?? "");
}

/**
 * What the one control is doing, in the founder's own terms.
 *
 * Muted and unmuted are the model. The other four exist because each of them
 * used to be drawn as one of those two and lie about it:
 *
 *  - `arming` was green. Green came from `enabled`, which is the switch, not
 *    from `listening`, which is the microphone — so the control claimed to be
 *    listening while the mic was still opening or a probe was in flight. That
 *    is the founder's bug: a green mic that heard nothing.
 *  - `spent` was grey, identical to muted. The listener stops itself at the
 *    cap and the switch goes off with no error, so "I ran out of budget" and
 *    "you turned me off" were the same pixels.
 *  - `refused` is a refusal to arm, not a choice.
 *  - `recording` is push-to-talk, which is a different conversation.
 */
export function micState(
  wake: MicWake | null | undefined,
  recording: boolean
): MicState {
  if (recording) return "recording";
  if (!wake) return "muted";
  if (wake.justHeard) return "heard";
  if (wake.error && !wake.enabled) return "refused";
  if (wake.enabled) return wake.listening ? "unmuted" : "arming";
  return spentBudget(wake) ? "spent" : "muted";
}

/**
 * Is the microphone actually open, for icon purposes?
 *
 * Muted, spent and refused all draw the struck-through microphone: they are
 * different reasons, but from where the engineer sits they are one fact --
 * nobody is listening. The reason is carried by the title, in words.
 */
export function micIsOpen(state: MicState): boolean {
  return state === "unmuted" || state === "heard" || state === "recording";
}

/**
 * The quiet line for a window that was paid for and was not my name.
 *
 * The listener already collects every transcript it gets (`lastGlimpse`),
 * matched or not, and the control used to throw it away — so "I heard you,
 * that was not the wake word" and "I am not listening" looked identical. This
 * is the whole difference, in one grey sentence.
 */
export function glimpseLine(glimpse: string | null | undefined): string | null {
  const text = (glimpse ?? "").trim();
  if (!text) return null;
  if (text === "(no audio in that window)") return "I heard the room, but no words in it.";
  if (text.startsWith("engine:")) {
    return `The engine couldn’t take that clip — ${text.slice("engine:".length).trim()}`;
  }
  return `I heard “${text}” — that was not my name.`;
}

/**
 * Exactly what the control says in each state, in the first person.
 *
 * Pure and exported so the sentences are pinned by test rather than by
 * whoever last edited the JSX: these are the only words the founder gets when
 * the microphone does nothing, so a state that quietly reuses another state's
 * sentence is the bug, not a shortcut.
 */
export function micTitle(
  state: MicState,
  wake: (MicWake & { error: string | null }) | null | undefined,
  budget: string | null
): string {
  if (!wake) {
    return "Press and hold to talk to me. The words land in the field for you to check.";
  }
  // "one window" reads as a sentence; "1 window" reads as a counter, and this
  // is the one place the count is being explained rather than displayed.
  const windows = `${wake.cap === 1 ? "one" : wake.cap} four-second window${
    wake.cap === 1 ? "" : "s"
  }`;
  switch (state) {
    case "unmuted":
      return `I am listening for “Hey ${WAKE_WORD}”. Click to mute me.${
        budget ? ` ${budget} paid windows used this listen.` : ""
      } Press and hold to talk to me directly instead.`;
    case "arming":
      return `I am opening the microphone. I am not listening yet — I will say so when I am. Press and hold to talk to me right now instead.`;
    case "heard":
      return `I heard my name. What follows it goes into the field for you to read before anything is sent.`;
    case "spent":
      return `I listened, sent ${windows} to the engine, and did not hear my name. I stopped there rather than keep spending calls on a quiet room. Click to listen again, or press and hold to talk to me now.`;
    case "refused":
      return `I am muted. ${wake.error}`;
    default:
      return `I am muted — I am not listening for “Hey ${WAKE_WORD}”. Click to unmute; while unmuted I send four-second windows to the engine, one model call each, capped at ${wake.cap}. Press and hold to talk to me now without unmuting.`;
  }
}

export interface VoiceControlProps {
  baseUrl: string;
  token?: string;
  disabled?: boolean;
  /** Receives the transcript for the intent draft. Never submits anything. */
  onTranscript: (text: string) => void;
  /** The wake-word listener, owned by the page. Without it the mic is push-to-talk only. */
  wake?: WakeWord;
  /**
   * Am I talking right now? The bar already knows (it swaps its field out for
   * the length of a reply), so it passes its own answer down rather than
   * letting two subscriptions to the same fact disagree on screen. Left out,
   * this control subscribes for itself.
   */
  speaking?: boolean;
}

/**
 * The strip's one voice control: a mute button for "Hey Ada".
 *
 * Unmuted means I am listening for the wake word. Muted means I am not. That
 * is the whole model, and it replaced a pair of microphone-shaped affordances
 * — a push-to-talk mic and a separate ear toggle — that between them could not
 * answer "so what does the other one do".
 *
 * Three things this control must never stop saying:
 *
 *  - **Unmuted costs money and is capped.** The budget is on the face of the
 *    control (`3 / 15`), it comes from the listener's own counter, and the
 *    control starts muted every time until someone turns it on.
 *  - **Refused is not muted.** If the webview cannot measure the room's
 *    loudness the listener will not arm — it refuses rather than sending clips
 *    it cannot gate — and the control says that in the listener's own words
 *    instead of sitting there looking like it is listening.
 *  - **Press and hold still records.** That is the path that reliably works,
 *    so it survives as a gesture on this same button rather than as a second
 *    control. Holding pauses the wake listener for the duration, because one
 *    microphone cannot be in two conversations.
 */
export const VoiceControl = ({
  baseUrl,
  token,
  disabled,
  onTranscript,
  wake,
  speaking: speakingProp,
}: VoiceControlProps) => {
  const [menuOpen, setMenuOpen] = useState(false);
  // Hooks run unconditionally; the prop only decides which answer is used.
  const liveSpeaking = useIsSpeaking();
  const speaking = speakingProp ?? liveSpeaking;
  const voice = useVoiceInput({ baseUrl, token, onTranscript });
  const holdRef = useRef<number | null>(null);
  // A hold has already done something; the click that follows it must not
  // also toggle the mute, or letting go of push-to-talk would arm the wake
  // word by accident.
  const heldRef = useRef(false);
  const resumeRef = useRef(false);
  // Held for as long as push-to-talk is held: while it exists I do not talk,
  // and anything I was saying was cut the moment it was taken. One room, one
  // microphone — a sentence of mine played into a live recording ends up in
  // the transcript as if the engineer had said it.
  const floorRef = useRef<(() => void) | null>(null);
  // Whether I am allowed to talk back. Read once and kept here so the menu's
  // switch and the flag in storage cannot disagree on screen.
  const [speaks, setSpeaks] = useState(isVoiceEnabled);

  useEffect(
    () => () => {
      if (holdRef.current !== null) window.clearTimeout(holdRef.current);
      floorRef.current?.();
      floorRef.current = null;
    },
    []
  );

  const recording = voice.status === "recording";
  const state = micState(wake, recording);

  // voice-orb lane, additive: holding this button stops the wake listener, so
  // `wake.listening` is false for the whole hold and anything reading only
  // that field draws a closed microphone while one is open. This is the one
  // place that knows better, so it says so.
  useEffect(() => {
    setPushToTalk(recording);
    return () => setPushToTalk(false);
  }, [recording]);

  const beginHold = () => {
    heldRef.current = true;
    floorRef.current?.();
    floorRef.current = takeMicFloor();
    if (wake?.listening) {
      resumeRef.current = true;
      wake.stop();
    }
    void voice.start();
  };

  const endHold = () => {
    if (holdRef.current !== null) {
      window.clearTimeout(holdRef.current);
      holdRef.current = null;
    }
    // Released even when the hold never became a recording: a floor left held
    // by a stray pointer event would mute me for the rest of the session.
    floorRef.current?.();
    floorRef.current = null;
    if (!heldRef.current) return;
    if (voice.status === "recording") voice.stop();
    if (resumeRef.current && wake?.enabled) wake.start();
    resumeRef.current = false;
    // Cleared on the next tick so the click this pointer-up produces is
    // swallowed rather than toggling the mute.
    window.setTimeout(() => {
      heldRef.current = false;
    }, 0);
  };

  const toggleMute = () => {
    if (heldRef.current) return;
    // While I am talking, this button shuts me up. It is the only control on
    // the strip, so it has to do the thing the strip is currently doing: the
    // ear is already closed by the duck for the length of every reply, so
    // toggling the mute here would be a click nobody can hear — the founder's
    // "there is no way to shut Ada up". Releasing the click un-ducks the ear
    // on its own (`announce`), so the mute is untouched either way.
    if (speaking) {
      speaker.stop();
      return;
    }
    if (!wake) {
      // No wake word in this build: the button is push-to-talk only, and a
      // tap is start/stop rather than a mute that would toggle nothing.
      if (recording) voice.stop();
      else void voice.start();
      return;
    }
    wake.setEnabled(!wake.enabled);
  };

  /**
   * The other switch, and the copy around it never blurs the two: the
   * microphone decides whether I HEAR you, this decides whether I TALK BACK.
   * They live in one menu because that is where someone goes to make the
   * voice stop, and they are worded as two sentences about two organs
   * because "mute" for both would leave no way to ask for only one.
   */
  const toggleSpeaks = () => {
    const next = !speaks;
    setSpeaks(next);
    saveVoiceEnabled(next);
    // Silencing means stop talking, not finish the sentence first.
    if (!next) speaker.stop();
  };

  // The cost, still counted, no longer printed on the face of the strip.
  // `2 / 10` beside a microphone reads as a meter someone is being scored
  // against; nobody asked for a score. It stays knowable in the two places
  // that are asked for rather than glanced at — the tooltip (micTitle) and
  // the menu — because a paid path with no visible cost anywhere is a worse
  // bug than a counter nobody wanted.
  const budget =
    wake && wake.backend === "windows" && state !== "muted" && state !== "recording"
      ? `${wake.sent} / ${wake.cap}`
      : null;

  // Speaking is not a microphone state and is deliberately not folded into
  // `micState`: it is a fact about the other organ, it lasts only as long as
  // an utterance, and the mute underneath it is unchanged the whole time. It
  // overrides only the two things the click actually changes — what the
  // button says and what it draws.
  const title = speaking
    ? "I’m talking — click to stop me mid-sentence. Your mute is unchanged."
    : micTitle(state, wake, budget);
  // Not `spent`: that state draws its own line, and the two used to render
  // together -- two `max-w-52` spans plus the mic inside a `shrink-0`
  // wrapper is ~460px that cannot compress, which is what pushed Generate
  // and the dashboard button off a 600px card. At most one status line.
  const glimpse =
    wake && (state === "unmuted" || state === "arming")
      ? glimpseLine(wake.lastGlimpse)
      : null;

  if (recording) {
    return (
      <div
        className="strip-control flex shrink-0 items-center gap-1 border border-[var(--strip-field-line)] pl-2.5 pr-1"
        data-testid="voice-recording"
      >
        <span className="size-2 shrink-0 animate-pulse rounded-full bg-[var(--strip-bad)]" />
        <span
          className="strip-mono text-[10.5px] tabular-nums text-[var(--strip-fg-2)]"
          data-testid="voice-elapsed"
        >
          {voice.elapsedS}s/{MAX_RECORDING_S}s
        </span>
        <Button
          variant="ghost"
          size="icon"
          className="size-6"
          onClick={voice.stop}
          title="Stop and transcribe"
          data-testid="voice-stop"
        >
          <CheckIcon className="size-3.5" />
        </Button>
        <Button
          variant="ghost"
          size="icon"
          className="size-6"
          onClick={voice.cancel}
          title="Throw this recording away"
          data-testid="voice-cancel"
        >
          <XIcon className="size-3.5" />
        </Button>
      </div>
    );
  }

  return (
    <Popover open={menuOpen} onOpenChange={setMenuOpen}>
      <PopoverAnchor asChild>
        <span
          className="relative flex min-w-0 shrink items-center gap-1"
          data-testid="voice-control"
          data-state={state}
          onContextMenu={(event) => {
            event.preventDefault();
            setMenuOpen(true);
          }}
        >
          <Button
            variant="ghost"
            size="icon"
            // A circle the height of every other control, with a surface of
            // its own: the bare glyph that was here read as a decoration
            // rather than a target. See .strip-mic.
            className={cn("strip-mic shrink-0 p-0")}
            style={{
              // Shape carries mute, not colour: a struck-through microphone is
              // what every call app means by muted, and it reads at a glance
              // without asking anyone to learn that grey-versus-green is the
              // difference. Colour is left for the one state that is a fault.
              color:
                state === "refused" ? "var(--strip-bad)" : "var(--strip-fg)",
            }}
            disabled={disabled || voice.status === "transcribing"}
            title={title}
            aria-label={title}
            aria-pressed={wake ? !wake.enabled : undefined}
            onClick={toggleMute}
            onPointerDown={() => {
              heldRef.current = false;
              holdRef.current = window.setTimeout(beginHold, HOLD_MS);
            }}
            onPointerUp={endHold}
            onPointerLeave={endHold}
            data-testid="voice-start"
            data-state={state}
            // Which organ this click closes, so the guarantee is pinnable by
            // test rather than by reading the handler: never "mic" while a
            // sentence is playing.
            data-stops={speaking ? "voice" : "mic"}
          >
            {voice.status === "transcribing" ? (
              <Loader2Icon className="size-4 animate-spin" />
            ) : speaking ? (
              // A speaker, not a microphone: for the length of a reply this
              // button is about my voice, and drawing a mic here would offer
              // to mute an ear the duck has already shut.
              <Volume2Icon className="size-[17px]" />
            ) : (
              // Two glyphs, the convention every call app already taught:
              // struck-through means I am not listening, plain means I am.
              // States that cannot listen -- muted, budget spent, refused --
              // all show the struck-through mic, because from where the
              // engineer sits they are the same fact. Which of them it is is
              // carried by aria-pressed and by the title in words.
              micIsOpen(state) ? (
                <MicIcon className="size-[17px]" />
              ) : (
                <MicOffIcon className="size-[17px]" />
              )
            )}
          </Button>

          {state === "refused" && wake?.error ? (
            <span
              className="max-w-52 truncate text-[10.5px] text-[var(--strip-bad)]"
              title={wake.error}
              data-testid="voice-refused"
            >
              {wake.error}
            </span>
          ) : null}

          {state === "heard" && wake ? (
            <span
              className="max-w-44 truncate text-[10.5px] text-[var(--strip-fg-2)]"
              data-testid="voice-heard"
            >
              {wake.lastHeard
                ? `heard “${WAKE_WORD}, ${wake.lastHeard}”`
                : `heard “${WAKE_WORD}” — tell me what you need`}
            </span>
          ) : null}

          {/* The cap is not a failure and it is not a mute: it is me having
              spent the one window I am allowed and stopping on purpose. Said
              here, because going quietly grey is what made the founder think
              nothing had happened at all. */}
          {state === "spent" ? (
            <span
              className="max-w-52 truncate text-[10.5px] text-[var(--strip-fg-2)]"
              title={title}
              data-testid="voice-spent"
            >
              {`I stopped after ${wake?.cap ?? 1} window${wake?.cap === 1 ? "" : "s"} — click to listen again`}
            </span>
          ) : null}

          {/* A window I paid for that was not my name. Quiet, and never where
              the wake line goes, so "listening", "heard the wrong words" and
              "stopped" are three different sights. */}
          {glimpse ? (
            <span
              className="max-w-52 truncate text-[10.5px] text-[var(--strip-fg-3)]"
              title={glimpse}
              data-testid="voice-glimpse"
            >
              {glimpse}
            </span>
          ) : null}
        </span>
      </PopoverAnchor>

      <PopoverContent
        align="end"
        side="bottom"
        sideOffset={8}
        className="w-80 border border-input/50 p-3"
        data-testid="voice-menu"
      >
        <div className="flex flex-col gap-2">
          <span className="text-[12px] font-medium">The microphone</span>
          {wake ? (
            <>
              <p className="text-[11px] leading-tight text-[var(--strip-fg-2)]">
                Click it to mute or unmute me. Unmuted I listen for “Hey{" "}
                {WAKE_WORD}”, and I start muted every time. When your machine
                gives me a recognizer of its own that costs nothing; otherwise
                every four seconds of room noise is one model call, capped at{" "}
                {wake.cap} per listen, and I stop and say so at the cap.
              </p>
              <p
                className="text-[11px] leading-tight text-[var(--strip-fg-3)]"
                data-testid="voice-menu-state"
              >
                {state === "unmuted"
                  ? `I am listening${budget ? `, ${budget} paid windows used` : ""}.`
                  : state === "arming"
                    ? "I am opening the microphone. I am not listening yet."
                    : state === "spent"
                      ? `I stopped after ${wake.cap} window${wake.cap === 1 ? "" : "s"} without hearing my name.`
                      : state === "refused"
                        ? `I am muted. ${wake.error}`
                        : "I am muted."}
                {glimpseLine(wake.lastGlimpse) && state !== "heard"
                  ? ` ${glimpseLine(wake.lastGlimpse)}`
                  : ""}
              </p>
              {/* The counter that used to sit on the strip. It is off the face
                  because it read as a score; it is here, unconditionally,
                  because the money is real and has to be findable. */}
              {wake.backend === "windows" ? (
                <p
                  className="text-[11px] leading-tight text-[var(--strip-fg-3)]"
                  data-testid="voice-menu-budget"
                  data-sent={wake.sent}
                  data-cap={wake.cap}
                >
                  {`${wake.sent} of ${wake.cap} paid windows used this listen, one model call each.`}
                </p>
              ) : null}
            </>
          ) : null}
          <p className="text-[11px] leading-tight text-[var(--strip-fg-2)]">
            Press and hold it to talk to me right now, muted or not. That is
            the path that always works: I record while you hold, and the words
            land in the field for you to read before anything is sent.
          </p>

          {/* The second switch. Separated by a rule and given its own heading
              because the microphone above it and this are opposite
              directions: one is whether I hear you, this is whether I talk
              back, and a menu that called both of them "mute" would leave no
              way to ask for only one of them. */}
          <div className="mt-1 flex flex-col gap-1.5 border-t border-input/40 pt-2">
            <span className="text-[12px] font-medium">My voice</span>
            <p className="text-[11px] leading-tight text-[var(--strip-fg-2)]">
              Separate from the microphone above: that one is whether I hear
              you, this one is whether I say anything back. I speak when a
              stage lands and is waiting for you, when something fails, and
              when I answer a question — never a running commentary. It costs
              nothing: the voice is your machine&rsquo;s own.
            </p>
            <Button
              variant="ghost"
              size="sm"
              className="h-7 justify-start gap-2 px-2 text-[11px]"
              onClick={toggleSpeaks}
              // Both halves in the label, so the button says which of the two
              // switches it is even when read on its own by a screen reader.
              title={
                speaks
                  ? "I speak at the moments that need an answer. Click to silence my voice; the microphone is unaffected."
                  : "My voice is silenced — I still hear you, I just say nothing back. Click to let me speak."
              }
              aria-label={
                speaks ? "Silence my voice" : "Let me speak"
              }
              aria-pressed={speaks}
              data-testid="voice-speak-toggle"
              data-enabled={speaks ? "1" : "0"}
            >
              {speaks ? (
                <Volume2Icon className="size-3.5" />
              ) : (
                <VolumeXIcon className="size-3.5 text-[var(--strip-fg-3)]" />
              )}
              {/* Shape and words both carry it: the icon changes AND the
                  sentence changes, because colour alone never states a state. */}
              <span data-testid="voice-speak-state">
                {speaks ? "I speak — click to silence me" : "Silenced — click to let me speak"}
              </span>
            </Button>
          </div>
        </div>
      </PopoverContent>
    </Popover>
  );
};
