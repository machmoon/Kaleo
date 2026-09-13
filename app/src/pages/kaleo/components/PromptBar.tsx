import { CornerDownLeftIcon, XIcon } from "lucide-react";
import { Button, Input } from "@/components";
import type { EngineHealth } from "@/hooks";
import { useSilkscreenRun, type RunRequestDraft } from "@/contexts";
import type { WakeWord } from "@/hooks/useWakeWord";
import type { SummaryMode } from "@/lib/silkscreen/types";
import { barContent } from "@/lib/overlay-mode";
import { useIsSpeaking } from "@/hooks/useIsSpeaking";
import { WAKE_WORD } from "@/lib/wake-word";
import { VoiceControl, micIsOpen, micState, type MicState } from "./VoiceControl";

// There is no status indicator in this row, and that is the whole design.
// The row is: field, microphone, ⏎ — and the two controls the page puts
// around it (collapse on the left, dashboard then drag handle on the right).
// Six things, in that order, in both bar sizes as far as each has them.
// A status dot lived here, then a living orb replaced it; both were removed
// on 2026-09-08 because a permanent indicator at the head of a 58px strip is
// the first thing the eye lands on and, resting, it says nothing.
//
// Nothing became invisible with them. An engine that is not answering puts
// "The engine is not answering; a run would fail immediately" on the ⏎
// control's own label (`submitReason` below) and raises the full-width
// engine-down banner in the card (`index.tsx`, `engine-down-reason`), which
// is a sentence at the width of the window rather than eight pixels of hue.
// The microphone draws its own state, because it is the organ that has one.

/**
 * What the strip says where the field was, in the first person.
 *
 * Pure and exported so the sentences are pinned by test: this is the only
 * copy the founder gets while the field is out of the way, and every line
 * has to be something the microphone is actually doing. There is no line
 * here for a closed microphone, because the panel never renders for one.
 *
 * It deliberately does not repeat the words it heard. Those already live
 * beside the microphone (VoiceControl's `voice-heard` and `voice-glimpse`),
 * and the same sentence printed twice on a 600px strip reads as two events.
 */
export function listeningLine(state: MicState, speaking = false): string {
  // Checked before the microphone states, because the speech duck closes the
  // microphone while I talk: at that instant `micOpen` is false and saying
  // "I'm listening" would be a lie about a mic that is shut.
  // It names the microphone because the microphone is the one control on the
  // strip and, while I talk, clicking it stops me. Naming a Stop button here
  // is what the removed one was: a second affordance for an organ the ear's
  // own button already covers.
  if (speaking) return "I’m talking — click the mic to stop me.";
  if (state === "heard") return "I heard my name — tell me what you need.";
  // No `recording` line, deliberately: push-to-talk is still owned by
  // VoiceControl, which swaps itself for its own recording strip, so this
  // panel never sees that state. A sentence for a state that cannot reach
  // here is copy nobody can check.
  return `I’m listening for “Hey ${WAKE_WORD}”.`;
}

/**
 * The listening state, in the field's own place and at the field's own size.
 *
 * What I am doing, and nothing else. It must never be taller than the input
 * it stands in for — the whole point is that the strip does not move.
 *
 * It carries no button. It used to end in a Stop that meant two different
 * things depending on which organ was open, which is one control too many for
 * a 58 px row and read as chrome the moment the microphone opened. Both of
 * the things it stopped now live on the microphone beside it: click it while
 * I talk and I stop, click it while I listen and the ear closes.
 */
const ListeningPanel = ({
  state,
  speaking,
}: {
  state: MicState;
  speaking: boolean;
}) => {
  return (
    <div
      // `kv-swap-in` and not `kv-settle`: this is a substitution, not an
      // arrival. The panel stands in the field's own slot at the field's own
      // height, so it must fade *through* it rather than rise into it — a
      // transform here would make the two cross past each other. See §1 of
      // src/motion.css.
      className="kv-swap-in flex h-9 min-w-0 flex-1 items-center gap-2 rounded-md border border-input/50 bg-muted/30 px-3"
      data-testid="prompt-listening"
      data-state={speaking ? "speaking" : state}
      role="status"
    >
      {/* voice-orb lane: the live indicator goes here. Until it exists this
          panel carries words only — a static shape would be decoration, and
          the animated one is theirs to draw. */}
      <span className="min-w-0 flex-1 truncate text-[12px] leading-tight">
        {listeningLine(state, speaking)}
      </span>
    </div>
  );
};

/** Everything that decides what the ⏎ control promises. */
export interface SubmitState {
  /** The field's contents, untrimmed. */
  intent: string;
  /** The engine answered its last probe. */
  engineOk: boolean;
  /** A run is in flight in either state machine. */
  busy: boolean;
  /** A step run is holding for approval. */
  awaitingApproval: boolean;
}

/**
 * What the ⏎ control will do, in one sentence, for the tooltip and the
 * accessible name.
 *
 * This function is the whole reason a glyph is allowed here. The button used
 * to say "Generate" or "Send" and carry the money warning in a title beside
 * the word; with the word gone, the title is the only copy left, so it is
 * pure, exported, and pinned by test rather than being prose inside JSX.
 *
 * Order matters and is deliberate. The mid-run case is answered first because
 * it is the one that changes what the button *costs*: it does not spend, and
 * saying "costs money" over a keystroke that only parks a sentence would be
 * the same lie in the other direction. Nothing was dropped — the engine and
 * empty-field reasons are still here, in their old words.
 */
export function submitReason(state: SubmitState): string {
  if (!state.intent.trim()) {
    if (state.busy) return "Say what you want changed and I will hold it until this run lands";
    return state.awaitingApproval
      ? "Answer the stage waiting above — “go” approves it"
      : "Type what you want on the board first";
  }
  // Spends nothing: the sentence is held until the run in flight lands, and
  // then offered. This is checked before the engine probe on purpose —
  // holding a sentence works whether or not the engine is answering.
  if (state.busy) return "Hold this until the run in flight lands — nothing is spent";
  if (!state.engineOk) return "The engine is not answering; a run would fail immediately";
  return state.awaitingApproval
    ? "Send this to the run in progress"
    : "Generate a board — this calls the model and costs money";
}

export interface PromptBarProps {
  request: RunRequestDraft;
  onRequestChange: (patch: Partial<RunRequestDraft>) => void;
  onSubmit: () => void;
  onCancel: () => void;
  /** The state machine's own guard: an in-flight run makes this false. */
  canStart: boolean;
  /** A run is in flight. */
  busy: boolean;
  /** The overlay is hidden by the global shortcut; keep focus out of it. */
  hidden: boolean;
  /**
   * True when cancelling reaches the engine — a step session exists, so the
   * run can be closed to further steps, which is where the money is.
   *
   * False for the two cases where all the button can do is stop this client
   * waiting: a one-shot run, and the first phase of a step run (`POST /steps`
   * is synchronous and there is no session id until it returns — and that is
   * the most model-expensive phase there is). The label says which, because
   * "nothing more is charged" is simply untrue of a model call already in
   * flight.
   */
  cancelReaches?: boolean;
  engine: EngineHealth;
  baseUrl: string;
  /** See RunOptions: routes the submit to the step-by-step KiCad flow. */
  stepMode?: boolean;
  onStepModeChange?: (enabled: boolean) => void;
  /**
   * See RunOptions: how the finished run summarises itself. Passed straight
   * through, like `stepMode` — the bar owns neither, it only carries them to
   * the options popover, and the page folds this one into the request it
   * starts.
   */
  summary?: SummaryMode;
  onSummaryChange?: (mode: SummaryMode) => void;
  /**
   * A step run is open and holding for approval, so what is typed here is a
   * reply to it rather than a new board. The bar says so, because the only
   * place that behaviour was written down was the code that implements it.
   */
  awaitingApproval?: boolean;
  /** The imperative for the next approvable stage, e.g. "Route copper". */
  nextAction?: string | null;
  /**
   * Where a transcript goes. The default drops it in the draft for the user to
   * read and edit. The page overrides it while a stage waits, so speech can
   * arm an approval — and only arm it. Nothing spoken has ever reached the
   * submit path and nothing here may change that.
   */
  onTranscript?: (text: string) => void;
  /**
   * The "Hey Ada" listener, owned by the page (see useWakeWord). It is handed
   * to the one microphone in the row, which is its mute control; nothing here
   * submits, and the ear only ever arms listening.
   */
  wake?: WakeWord;
  /** Test seam for the speech poll; production reads the module speaker. */
  isSpeaking?: () => boolean;
}

export const PromptBar = ({
  request,
  onRequestChange,
  onSubmit,
  onCancel,
  canStart,
  busy,
  hidden,
  cancelReaches = false,
  engine,
  baseUrl,
  awaitingApproval = false,
  nextAction = null,
  onTranscript,
  wake,
  isSpeaking,
}: PromptBarProps) => {
  // `canStart` is still the guard on *spending*: it is false while a run is in
  // flight. Submitting is wider than spending now — a sentence typed mid-run
  // is parked by the page, which costs nothing — so the control is live
  // whenever the bar is on screen, and the page decides what the sentence
  // does. The one thing that must not happen is a second paid run starting
  // without a click on a button that said it would; that lives in the page.
  const submittable = (canStart || busy) && !hidden;
  // What cancelling actually does, which is not the same in the two cases and
  // must never claim the second one. A model call already in flight is paid
  // for whether or not anyone is waiting for it, and the placement solve runs
  // its full budget; what a real cancel buys is the steps that now never run.
  const cancelDetail = cancelReaches
    ? "Close this run: no further step will run. Work already under way — the placement solve, a model call in flight, the background case and parts jobs — finishes and is discarded."
    : "Stop waiting on this. It does not stop the work: a model call already in flight is paid for either way, and this run has no session yet to close.";
  const reason = submitReason({
    intent: request.intent,
    engineOk: engine.ok,
    busy,
    awaitingApproval,
  });
  // Am I talking? Its own fact, published by the speech layer, and not the
  // microphone's: the duck closes the mic for the length of every reply, so
  // without this the field would flicker back into the strip mid-sentence.
  // The hook is called unconditionally — the seam overrides its answer, it
  // does not replace the call, or the hook order would change with a prop.
  const live = useIsSpeaking();
  const speaking = isSpeaking ? isSpeaking() : live;
  // Only for the bearer token — the run itself stays behind onSubmit.
  const { token } = useSilkscreenRun();

  // While a stage waits, this field is a conversation with the run: "go"
  // approves, "start over" restarts. Nothing typed here starts a second
  // board, so the control says Send rather than Generate.
  const placeholder = awaitingApproval
    ? nextAction
      ? `Say \u201cgo\u201d to ${nextAction.toLowerCase()}, or ask for a change`
      : "Ask for a change, or describe a different board"
    : busy
      ? "Say what you want changed \u2014 I\u2019ll hold it until this lands"
      : "What do you need built?";

  // While the microphone is open the field steps aside and the listening
  // state stands in its place, at the same size. `micState` reads the
  // microphone, not the switch, so this can never claim to be listening
  // while the mic is still opening. Nothing here touches request.intent:
  // the draft lives in the parent, so the text is exactly where it was when
  // the field comes back.
  const listenState = micState(wake, false);
  const showListening =
    barContent({ micOpen: micIsOpen(listenState), speaking, hidden }) === "listening";

  return (
    // `min-w-0` is load-bearing: a flex item's default min-width is `auto`,
    // so without it this row refuses to shrink below its own content and
    // pushes Generate, the dashboard button and the drag handle off the
    // 600px card whenever a status line beside the mic gets long. Seen on
    // screen with the glimpse line showing.
    <div className="flex min-w-0 flex-1 items-center gap-1.5">
      {showListening ? (
        <ListeningPanel state={listenState} speaking={speaking} />
      ) : (
        <Input
          placeholder={placeholder}
          value={request.intent}
          // Typing is no longer switched off while a run is in flight. A
          // disabled field is the app refusing a sentence before it has heard
          // it; what the sentence costs is decided when it is submitted, and
          // mid-run it costs nothing — the page parks it.
          disabled={hidden}
          data-testid="prompt-input"
          // The other half of the same substitution; see ListeningPanel.
          className="kv-swap-in flex-1"
          onChange={(e) => onRequestChange({ intent: e.target.value })}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && submittable) {
              e.preventDefault();
              onSubmit();
            }
          }}
        />
      )}

      {/* One microphone, and it is the row's only voice control. Tapping it
          mutes and unmutes "Hey Ada"; tapping it while I am talking stops me;
          holding it is push-to-talk. `speaking` is passed rather than
          subscribed so the panel's sentence and the button's job cannot
          disagree about whether a reply is playing. */}
      <VoiceControl
        baseUrl={baseUrl}
        token={token}
        disabled={busy || hidden}
        wake={wake}
        speaking={speaking}
        // The transcript joins the draft through the same onChange path typing
        // uses, for the user to review and edit. It must never submit.
        onTranscript={(text) =>
          onTranscript
            ? onTranscript(text)
            : onRequestChange({
                intent: request.intent.trim()
                  ? `${request.intent.trimEnd()} ${text}`
                  : text,
              })
        }
      />

      {/* The run-options gear used to sit here. It is out of the row on
          purpose: the strip is meant to be one microphone and one way to
          submit, and a gear in a 58 px bar reads as chrome. The same
          `RunOptions` form is mounted in the card below (index.tsx,
          `run-options-disclosure`), so the solver budget, the datasheets, the
          step-by-step switch and the summary mode are all still reachable —
          they are just no longer in the way. */}

      {/* Cancel comes BEFORE the arrow, so the arrow is the last control in
          the row in every state — see the note on the arrow below. */}
      {busy ? (
        <span title={cancelDetail}>
          <Button
            variant="outline"
            size="icon"
            onClick={onCancel}
            aria-label={cancelDetail}
            data-testid="prompt-cancel"
            data-reaches={String(cancelReaches)}
          >
            <XIcon className="size-3.5" />
          </Button>
        </span>
      ) : null}

      <span
        // The title lives on a wrapper because a disabled element suppresses
        // pointer events in some webviews — the one place the reason lived
        // was the one place it could never show.
        //
        // The control is a glyph now, so this string is the only place the
        // money warning and the refusal reasons are written down. Every
        // sentence that was reachable here still is, plus the mid-run one,
        // and `submitReason` is exported so a test reads the same function
        // the button does rather than a copy of its prose.
        title={reason}
      >
        <Button
          size="sm"
          onClick={onSubmit}
          disabled={!submittable}
          // A glyph-only button reads as "corner down left" without this.
          aria-label={reason}
          // This is the control that spends money — except while a run is in
          // flight, when what it does instead is hold the sentence, and the
          // label says which.
          data-testid="prompt-submit"
          // What it will do with the field: `start` spends a pipeline, `reply`
          // answers the stage that is waiting, `park` spends nothing. Pinned
          // by test so the promise is checked without reading prose.
          data-intent={busy ? "park" : awaitingApproval ? "reply" : "start"}
        >
          <CornerDownLeftIcon className="size-3.5" />
        </Button>
      </span>
    </div>
  );
};
