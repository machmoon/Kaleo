interface ProgressDotsProps {
  index: number;
  total: number;
}

/** Six 6 px dots, 8 px apart. Decorative — the layout's aria-live names the step. */
export const ProgressDots = ({ index, total }: ProgressDotsProps) => (
  <div className="flex items-center justify-center gap-2" aria-hidden="true" data-testid="setup-dots">
    {Array.from({ length: total }, (_, i) => (
      <span
        key={i}
        data-active={i === index ? "true" : "false"}
        className={`size-1.5 rounded-full ${i === index ? "bg-foreground" : "bg-muted-foreground/30"}`}
      />
    ))}
  </div>
);
