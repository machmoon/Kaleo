import { ChevronRightIcon } from "lucide-react";
import { Button, DragButton } from "@/components";
import { useIsSpeaking } from "@/hooks/useIsSpeaking";
import type { WakeWord } from "@/hooks/useWakeWord";
import { VoiceControl } from "./VoiceControl";

export interface CompactBarProps {
  baseUrl: string;
  token?: string;
  /** The overlay is hidden by the global shortcut; keep focus out of it. */
  hidden: boolean;
  /** Open the full bar (the arrow). */
  onExpand: () => void;
  /**
   * A transcript landed. The page puts it in the draft and opens the bar so
   * the user can read and edit it; nothing here can reach the submit path.
   */
  onTranscript: (text: string) => void;
  /**
   * The "Hey Ada" listener, owned by the page. The pill's microphone is its
   * mute control, so folding the strip away does not take the ear with it.
   */
  wake?: WakeWord;
}

/**
 * The overlay folded down: a pill exactly three controls wide.
 *
 * Arrow, microphone, drag handle — and nothing has permission to appear
 * between them. The full bar's row is the same list with a field and a ⏎ in
 * the middle, so a control never changes side when the strip folds.
 *
 * It is not the default (the full bar is; see index.tsx) because on its own
 * it was too small to be useful.
 */
export const CompactBar = ({
  baseUrl,
  token,
  hidden,
  onExpand,
  onTranscript,
  wake,
}: CompactBarProps) => {
  // The mic ducks itself for the length of every reply; it needs to know so
  // that tapping it stops me rather than reopening the ear.
  const speaking = useIsSpeaking();

  return (
    // `kv-shape-in` (motion.css §2): the pill is what mounts when the bar
    // collapses, so it carries the arrival — 4 px in from the left with the
    // opacity behind it, along the same axis the native window resizes on.
    // It sits here, on CompactBar's own root, rather than on the wrapper
    // `useOverlayHeight` observes: a transform cannot change that wrapper's
    // border box, `scrollWidth` or `scrollHeight`, so the measurement never
    // sees a mid-animation size and the window is never resized by motion.
    <div className="kv-shape-in flex items-center gap-1" data-testid="compact-bar">
      {/* Arrow, microphone, handle — the same three slots in the same order
          as the full bar's first three (collapse, field, mic), so folding
          the strip down never moves a control sideways. The arrow is always
          an arrow: a listening sentence used to take this slot and push the
          microphone along with it, which is the moving-control complaint in
          its smallest form. What the microphone is doing, the microphone
          draws. */}
      <Button
        variant="ghost"
        size="icon"
        title="Open the prompt (type instead)"
        aria-label="Open the prompt"
        onClick={onExpand}
        data-testid="overlay-expand"
      >
        <ChevronRightIcon className="size-4" />
      </Button>
      <VoiceControl
        baseUrl={baseUrl}
        token={token}
        disabled={hidden}
        wake={wake}
        speaking={speaking}
        onTranscript={onTranscript}
      />
      <DragButton />
    </div>
  );
};
