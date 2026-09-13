import { useEffect, useRef, type ReactNode } from "react";

interface StepFrameProps {
  /** ≤ 5 words, sentence case. */
  title: string;
  /** ≤ 2 lines. */
  subtitle: string;
  /** A stable id so the layout can move focus here on a step change. */
  stepId: string;
  children?: ReactNode;
  /** Something above the title: the brand mark on hello. */
  above?: ReactNode;
}

/**
 * The title block every wizard step shares.
 *
 * The title takes focus when the step mounts — a screen reader then reads the
 * new screen instead of the Continue button it was sitting on — and the
 * `aria-live` sentence is the layout's, not this component's, so it can name
 * the step number.
 */
export const StepFrame = ({ title, subtitle, stepId, children, above }: StepFrameProps) => {
  const titleRef = useRef<HTMLHeadingElement | null>(null);
  useEffect(() => {
    titleRef.current?.focus({ preventScroll: true });
  }, [stepId]);

  return (
    <section
      className="mx-auto flex w-full max-w-[520px] flex-col items-center text-center"
      data-testid="setup-step"
      data-step={stepId}
    >
      {above ? <div className="mb-5">{above}</div> : null}
      <h1
        ref={titleRef}
        tabIndex={-1}
        className="text-[28px] font-semibold tracking-tight outline-none"
        data-testid="setup-title"
      >
        {title}
      </h1>
      <p className="mt-2 text-[15px] leading-snug text-muted-foreground" data-testid="setup-subtitle">
        {subtitle}
      </p>
      {children ? <div className="mt-7 w-full">{children}</div> : null}
    </section>
  );
};
