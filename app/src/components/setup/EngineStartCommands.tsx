import { CheckIcon, CopyIcon, TerminalIcon } from "lucide-react";
import { Button } from "@/components/ui";
import { useCopyToClipboard } from "@/hooks/useCopyToClipboard";
import { ENGINE_START_STEPS, type EngineStartStep } from "@/config/kaleo.constants";

/** One quoted command with a copy button. Its own component for the hook. */
const CommandBlock = ({ command }: { command: string }) => {
  const { isCopied, handleCopy } = useCopyToClipboard({ text: command });
  return (
    <div className="flex items-start gap-2 rounded-xl border border-input/50 bg-muted/40 p-2.5">
      <TerminalIcon className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
      <code
        data-testid="engine-start-command"
        className="flex-1 font-mono text-[11px] lg:text-xs leading-relaxed break-all select-text"
      >
        {command}
      </code>
      <Button
        size="icon"
        variant="ghost"
        className="size-6 shrink-0"
        title={isCopied ? "Copied" : "Copy command"}
        aria-label={isCopied ? "Copied" : "Copy command"}
        onClick={handleCopy}
      >
        {isCopied ? <CheckIcon className="size-3" /> : <CopyIcon className="size-3" />}
      </Button>
    </div>
  );
};

const StartStep = ({ step, compact }: { step: EngineStartStep; compact: boolean }) => (
  <div data-testid="engine-start-step" data-step={step.id} className="space-y-1.5">
    <p className="text-sm font-medium">{step.title}</p>
    {compact ? null : (
      <p className="text-xs leading-relaxed text-muted-foreground">{step.detail}</p>
    )}
    <CommandBlock command={step.command} />
  </div>
);

interface EngineStartCommandsProps {
  /** Only these step ids, in catalogue order. Default: all of them. */
  only?: readonly EngineStartStep["id"][];
  /** Drop the long `detail` paragraphs: the wizard has a 620 px window. */
  compact?: boolean;
  className?: string;
}

/**
 * The commands that start the engine, with Copy. Shared by the Engine page
 * and the wizard's engine step, because the app cannot start Python itself
 * and both places have to say so with the same text.
 */
export const EngineStartCommands = ({ only, compact = false, className }: EngineStartCommandsProps) => {
  const steps = only ? ENGINE_START_STEPS.filter((s) => only.includes(s.id)) : ENGINE_START_STEPS;
  return (
    <div className={`space-y-4 ${className ?? ""}`} data-testid="engine-start-commands">
      {steps.map((step) => (
        <StartStep key={step.id} step={step} compact={compact} />
      ))}
    </div>
  );
};
