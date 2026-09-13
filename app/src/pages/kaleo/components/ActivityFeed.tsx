import { useEffect, useMemo, useRef } from "react";
import { ScrollArea } from "@/components";
import type { FeedLine } from "@/lib/silkscreen/describe";

/** The newest N lines are all that fit; a long run must not grow the overlay. */
const MAX_LINES = 200;

export interface ActivityFeedProps {
  /**
   * The describer's sentences, in arrival order.
   *
   * Every line here corresponds to a frame the engine actually sent —
   * `describe.ts` drops the events this build has never heard of rather than
   * inventing a sentence for them, so nothing in this list is narration.
   */
  lines: FeedLine[];
  className?: string;
}

export const ActivityFeed = ({ lines, className }: ActivityFeedProps) => {
  const visible = useMemo(() => lines.slice(-MAX_LINES), [lines]);

  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [visible.length]);

  // Nothing has arrived: render nothing rather than an empty box implying it did.
  if (visible.length === 0) return null;

  return (
    <ScrollArea className={className} data-testid="activity-feed">
      <div className="flex flex-col gap-0.5 pr-2">
        {visible.map((line) => (
          <div
            key={line.seq}
            className="flex gap-2 text-[11px] leading-relaxed"
            data-testid="activity-line"
            data-event={line.event}
          >
            <span className="w-8 shrink-0 text-right tabular-nums text-muted-foreground/60">
              {line.tS === null ? "" : line.tS.toFixed(1)}
            </span>
            <span className="text-muted-foreground">{line.text}</span>
          </div>
        ))}
        <div ref={endRef} />
      </div>
    </ScrollArea>
  );
};
