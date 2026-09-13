// The one microphone level, published by whoever already owns the analyser.
//
// The overlay must never open a second `AudioContext` to draw a level meter.
// One device, one tap: the wake listener already computes `peakDeviation` per
// ~100 ms analyser frame (`lib/wake-word.ts`), and push-to-talk records from a
// stream of its own. Opening another `getUserMedia` beside them is not a style
// choice — on macOS it is a second permission prompt, a second stream to leak,
// and a real chance of the two fighting over the input device.
//
// So this file is a *bus*, not a tap. Whoever holds the analyser calls
// `publishMicLevel(peak)`; anything that wants to draw the level subscribes.
// Nothing here ever touches the microphone.
//
// The other half of the design is staleness. A level that keeps its last
// value after the publisher stops would make an indicator wave at a closed
// microphone — the exact lie this whole feature exists to stop — so a reading
// older than `MIC_LEVEL_STALE_MS` is reported as *no signal*, and no signal
// is a distinct answer from a level of zero. A consumer with no signal is
// expected to degrade to a plain state rather than invent motion.

import { useEffect, useRef, useState } from "react";

/**
 * Peak deviation from silence in one analyser frame, 0-128, exactly what
 * `peakDeviation` in `lib/wake-word.ts` returns. The bus carries that raw
 * number rather than a normalised one so the publisher stays dumb and the
 * mapping stays in one testable place (`normaliseMicPeak`).
 */
export type MicPeak = number;

/**
 * After this long with no publish, there is no signal. Deliberately short:
 * the wake listener meters every 100 ms, so four missed frames means the
 * analyser is gone, not that the room went quiet.
 */
export const MIC_LEVEL_STALE_MS = 450;

/** Below this the room is silence, not speech. Matches the VAD floor's spirit. */
export const MIC_PEAK_FLOOR = 3;

/** Peak that counts as "full" on the meter. Normal speech peaks well under 128. */
export const MIC_PEAK_FULL = 48;

/**
 * Raw analyser peak (0-128) to a 0-1 meter reading.
 *
 * Pure, so the curve is pinned by test rather than by whatever looked nice in
 * one room. The exponent is perceptual: linear amplitude spends most of its
 * range on shouting, and an orb that only moves when you shout reads as
 * broken.
 */
export function normaliseMicPeak(peak: MicPeak): number {
  if (!Number.isFinite(peak)) return 0;
  const span = MIC_PEAK_FULL - MIC_PEAK_FLOOR;
  const raw = (peak - MIC_PEAK_FLOOR) / span;
  if (raw <= 0) return 0;
  if (raw >= 1) return 1;
  return raw ** 0.7;
}

type Frame = { peak: MicPeak; at: number };

let latest: Frame | null = null;
const subscribers = new Set<(frame: Frame) => void>();

/**
 * Report one measured frame. Called by the code that already has the
 * analyser open; see the patch note at the bottom of this file.
 */
export function publishMicLevel(peak: MicPeak, at: number = Date.now()): void {
  latest = { peak, at };
  for (const notify of subscribers) notify(latest);
}

/**
 * Forget the last reading — the microphone closed.
 *
 * Staleness alone would get there in `MIC_LEVEL_STALE_MS`, but a publisher
 * that knows it has stopped should say so immediately: half a second of an
 * indicator still reacting to a mic that is already released is half a second
 * of the lie.
 */
export function clearMicLevel(): void {
  latest = null;
  for (const notify of subscribers) notify({ peak: 0, at: 0 });
}

/** The last frame, or `null` when nothing has been published. Test seam. */
export function micLevelSnapshot(): Frame | null {
  return latest;
}

export interface MicLevel {
  /** Smoothed meter reading, 0-1. Always 0 when `hasSignal` is false. */
  level: number;
  /**
   * Is anything actually measuring the microphone right now? False means
   * "draw a plain state", never "the room is silent".
   */
  hasSignal: boolean;
}

export interface UseMicLevelOptions {
  /** Frame interval for the smoother, ms. */
  tickMs?: number;
  /** Skip the smoothing loop entirely (reduced motion). */
  still?: boolean;
  /** Test seam. */
  now?: () => number;
}

/**
 * Subscribe to the bus and smooth it into something drawable.
 *
 * Attack is instant and release is a decay: speech is spiky at 10 frames a
 * second, and an indicator that follows every frame down flickers rather than
 * breathes. Nothing here invents a value — with no publisher the hook reports
 * `hasSignal: false` and a level of zero, forever.
 */
export function useMicLevel(
  active: boolean,
  { tickMs = 33, still = false, now = Date.now }: UseMicLevelOptions = {}
): MicLevel {
  const [state, setState] = useState<MicLevel>({ level: 0, hasSignal: false });
  const targetRef = useRef(0);
  const valueRef = useRef(0);
  const lastAtRef = useRef(0);

  useEffect(() => {
    if (!active) {
      targetRef.current = 0;
      valueRef.current = 0;
      lastAtRef.current = 0;
      setState((prev) => (prev.level === 0 && !prev.hasSignal ? prev : { level: 0, hasSignal: false }));
      return;
    }
    const onFrame = (frame: Frame) => {
      lastAtRef.current = frame.at;
      targetRef.current = frame.at === 0 ? 0 : normaliseMicPeak(frame.peak);
      if (still) {
        // No smoothing loop to pick this up, so publish straight through.
        valueRef.current = targetRef.current;
        setState({
          level: targetRef.current,
          hasSignal: frame.at !== 0,
        });
      }
    };
    subscribers.add(onFrame);
    const seed = latest;
    if (seed && now() - seed.at <= MIC_LEVEL_STALE_MS) onFrame(seed);

    let timer: ReturnType<typeof setInterval> | null = null;
    if (!still) {
      timer = setInterval(() => {
        const fresh =
          lastAtRef.current !== 0 && now() - lastAtRef.current <= MIC_LEVEL_STALE_MS;
        if (!fresh) {
          // No measurement means no level — not a level that fades out. A
          // decaying tail after the microphone closed is motion nothing is
          // driving, which is the lie in miniature.
          valueRef.current = 0;
        } else {
          const target = targetRef.current;
          const current = valueRef.current;
          // Rise on the frame it happened; fall over ~150 ms, because speech
          // is spiky at ten frames a second and following it down flickers.
          valueRef.current = target > current ? target : current * 0.82 + target * 0.18;
          if (valueRef.current < 0.004) valueRef.current = 0;
        }
        setState((prev) =>
          prev.hasSignal === fresh && Math.abs(prev.level - valueRef.current) < 0.004
            ? prev
            : { level: valueRef.current, hasSignal: fresh }
        );
      }, tickMs);
    }
    return () => {
      subscribers.delete(onFrame);
      if (timer) clearInterval(timer);
    };
  }, [active, still, tickMs, now]);

  return state;
}

// ------------------------------------------------------------- push-to-talk
//
// The other way the microphone opens, and the one the level bus cannot see.
//
// Holding the mic button *stops* the wake listener (`VoiceControl.beginHold`
// calls `wake.stop()`) and records from a stream of its own, so `wake.
// listening` is false for the whole hold. An indicator reading only that
// field draws "microphone closed" while the engineer is physically holding
// the button down — the founder's bug inverted, and just as wrong.
//
// So push-to-talk announces itself here, on the same principle as the level:
// the code that owns the microphone is the only code allowed to say whether
// it is open.

let pushToTalk = false;
const pushSubscribers = new Set<(on: boolean) => void>();

/** Called by the one control that records: true while the recorder is open. */
export function setPushToTalk(on: boolean): void {
  if (pushToTalk === on) return;
  pushToTalk = on;
  for (const notify of pushSubscribers) notify(on);
}

/** Is push-to-talk recording right now? */
export function isPushToTalk(): boolean {
  return pushToTalk;
}

/** Subscribe to push-to-talk. Re-renders only the caller. */
export function usePushToTalk(): boolean {
  const [on, setOn] = useState(pushToTalk);
  useEffect(() => {
    setOn(pushToTalk);
    pushSubscribers.add(setOn);
    return () => {
      pushSubscribers.delete(setOn);
    };
  }, []);
  return on;
}
