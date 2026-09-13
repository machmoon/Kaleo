import kaleoMark from "@/assets/kaleo-mark.png";
import { StepFrame } from "@/components/setup";

export const HELLO_TITLE = "Hello.";

/** The brand mark is allowed here: this is the dashboard surface, not the strip. */
export const HelloStep = () => (
  <StepFrame
    stepId="hello"
    title={HELLO_TITLE}
    subtitle="Hardy is a junior hardware engineer who works beside KiCad. A few choices, then you are set."
    above={<img src={kaleoMark} alt="" width={72} height={72} className="mx-auto rounded-2xl" draggable={false} />}
  />
);
