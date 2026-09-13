import { useEffect, useState, type ReactNode } from "react";
import { Button } from "@/components/ui";
import { DemoBanner, ProgressDots } from "@/components/setup";
import { motionAttr, useReducedMotion } from "@/hooks/useReducedMotion";

export interface WelcomeLayoutProps {
  /** The current step's title, for the aria-live sentence. */
  title: string;
  index: number;
  total: number;
  demo: boolean;
  /** Hidden on hello; the done step has no footer at all. */
  showDots?: boolean;
  showFooter?: boolean;
  showBack?: boolean;
  canContinue?: boolean;
  continueLabel?: string;
  onContinue: () => void;
  onBack: () => void;
  /** "Set Up Later": skip this screen. */
  onLater: () => void;
  /** Shift+Esc, after the inline confirm: end the wizard now. */
  onExit: () => void;
  children: ReactNode;
}

/** Keys pressed inside these are the control's own business, not the wizard's. */
function ownsEnter(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "BUTTON" || tag === "A" || tag === "SELECT") {
    return true;
  }
  return target.isContentEditable || target.getAttribute("role") === "radio" || target.getAttribute("role") === "switch";
}

/**
 * The wizard's window: plain `bg-background`, a 40 px drag strip for the
 * traffic lights, the step in the middle, the footer at the bottom. The
 * shell sizes the window (820×620, non-resizable) — nothing here draws a
 * sheet or a border, and nothing here scrolls unless a step overflows.
 *
 * Keys: Enter = Continue, Esc = Set Up Later, Shift+Esc = end setup (with
 * an inline confirm, since it skips everything), ⌘← = Back.
 */
export const WelcomeLayout = ({
  title,
  index,
  total,
  demo,
  showDots = true,
  showFooter = true,
  showBack = true,
  canContinue = true,
  continueLabel = "Continue",
  onContinue,
  onBack,
  onLater,
  onExit,
  children,
}: WelcomeLayoutProps) => {
  const still = useReducedMotion();
  const [confirmExit, setConfirmExit] = useState(false);

  useEffect(() => {
    setConfirmExit(false);
  }, [index]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      if (event.key === "Escape") {
        event.preventDefault();
        if (event.shiftKey) {
          setConfirmExit(true);
        } else if (confirmExit) {
          setConfirmExit(false);
        } else {
          onLater();
        }
        return;
      }
      if (event.key === "ArrowLeft" && event.metaKey) {
        if (showBack) {
          event.preventDefault();
          onBack();
        }
        return;
      }
      if (event.key === "Enter" && !event.metaKey && !event.altKey && !event.ctrlKey) {
        if (ownsEnter(event.target)) return;
        if (!showFooter || !canContinue) return;
        event.preventDefault();
        onContinue();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [confirmExit, canContinue, showBack, showFooter, onBack, onContinue, onLater]);

  return (
    <div
      className="relative flex h-screen w-screen flex-col overflow-hidden bg-background text-foreground"
      data-testid="welcome-layout"
      data-motion={motionAttr(still)}
    >
      {/* Traffic lights sit at (14, 18) over the backdrop; this strip is what they drag. */}
      <div className="h-10 shrink-0 select-none" data-tauri-drag-region={true} />

      <p className="sr-only" aria-live="polite" data-testid="setup-live">
        {`Step ${index + 1} of ${total}, ${title}`}
      </p>

      {demo ? (
        <div className="mx-auto w-full max-w-[560px] px-6">
          <DemoBanner />
        </div>
      ) : null}

      <main className="flex min-h-0 flex-1 flex-col items-center justify-center overflow-y-auto px-12 py-4">
        {children}
      </main>

      {showDots ? (
        <div className="pb-5">
          <ProgressDots index={index} total={total} />
        </div>
      ) : null}

      {showFooter ? (
        <footer className="flex shrink-0 items-center justify-between gap-3 px-8 pb-8" data-testid="setup-footer">
          {confirmExit ? (
            <div className="flex items-center gap-2 text-sm" role="alertdialog" data-testid="setup-exit-confirm">
              <span className="text-muted-foreground">End setup now? Everything left is skipped.</span>
              <Button size="sm" variant="outline" onClick={onExit} data-testid="setup-exit-yes">
                End setup
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setConfirmExit(false)} data-testid="setup-exit-no">
                Keep going
              </Button>
            </div>
          ) : (
            <Button variant="ghost" onClick={onLater} data-testid="setup-later">
              Set Up Later
            </Button>
          )}
          <div className="flex items-center gap-2">
            {showBack ? (
              <Button variant="outline" onClick={onBack} data-testid="setup-back">
                Back
              </Button>
            ) : null}
            <Button onClick={onContinue} disabled={!canContinue} data-testid="setup-continue">
              {continueLabel}
            </Button>
          </div>
        </footer>
      ) : null}
    </div>
  );
};
