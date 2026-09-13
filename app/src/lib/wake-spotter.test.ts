import { describe, expect, it } from "vitest";
import {
  createAnalyserFeed,
  countSyllables,
  createWakeSpotter,
  frameFeatures,
  segmentSpeech,
  SPOTTER_FRAME_MS,
  wakeShape,
  type SpotterFrame,
  type WakeShape,
} from "./wake-spotter";
import { WAKE_CORPUS } from "./wake-corpus";

/**
 * The corpus is real speech, not invented numbers.
 *
 * `scratchpad/wake-corpus/feat.mjs` runs macOS `say` (three voices) into
 * 16 kHz mono WAV, mixes synthetic room noise over it and pads 700 ms of that
 * noise either side, then reduces each 20 ms frame to [rms, zcr] — exactly the
 * two numbers an `AnalyserNode` gives the ear at runtime. `wake-corpus.ts` is
 * that output, frozen. So these numbers are measured, and when a threshold
 * moves this file says which clips it cost.
 *
 * They are also synthesised speech from one vendor's voices: they bound the
 * classifier's behaviour, they do not predict a human in a room. The measured
 * rates below are the honest claim, and they are deliberately asserted rather
 * than described, so a change that quietly makes the filter worse fails here.
 */
function frames(name: string): SpotterFrame[] {
  return WAKE_CORPUS[name].map(([rms, zcr]) => ({ rms, zcr }));
}

/** Run one clip through the streaming spotter; did any utterance look wake-shaped? */
function spot(name: string): { accepted: boolean; utterances: WakeShape[] } {
  const utterances: WakeShape[] = [];
  const spotter = createWakeSpotter({ onUtterance: ({ shape }) => utterances.push(shape) });
  for (const frame of frames(name)) spotter.push(frame);
  return { accepted: utterances.some((s) => s.wakeShaped), utterances };
}

const POSITIVES = Object.keys(WAKE_CORPUS).filter((k) => k.startsWith("p_"));
const NEGATIVES = Object.keys(WAKE_CORPUS).filter((k) => k.startsWith("n_"));

describe("frameFeatures", () => {
  it("reads silence as zero on both axes", () => {
    const silence = new Uint8Array(256).fill(128);
    expect(frameFeatures(silence)).toEqual({ rms: 0, zcr: 0 });
  });

  it("measures a square wave's amplitude and its rate", () => {
    // 256 samples, 8 samples per half period => 32 half periods => 31 crossings.
    const wave = new Uint8Array(256);
    for (let i = 0; i < wave.length; i += 1) wave[i] = Math.floor(i / 8) % 2 ? 148 : 108;
    const { rms, zcr } = frameFeatures(wave);
    expect(rms).toBeCloseTo(20, 5);
    expect(zcr).toBeCloseTo(31 / 256, 5);
  });

  it("is empty-safe", () => {
    expect(frameFeatures([])).toEqual({ rms: 0, zcr: 0 });
  });
});

describe("countSyllables", () => {
  it("counts one hump as one", () => {
    expect(countSyllables([2, 6, 10, 12, 10, 6, 2])).toBe(1);
  });

  it("counts two humps split by a real valley as two", () => {
    expect(countSyllables([2, 10, 12, 10, 3, 2, 3, 10, 12, 10, 2])).toBe(2);
  });

  it("does not count a wobble on one nucleus as two", () => {
    expect(countSyllables([2, 10, 12, 11, 12, 10, 2])).toBe(1);
  });

  it("is empty- and silence-safe", () => {
    expect(countSyllables([])).toBe(0);
    expect(countSyllables([0, 0, 0])).toBe(0);
  });
});

describe("segmentSpeech", () => {
  const quiet = (n: number): SpotterFrame[] =>
    Array.from({ length: n }, () => ({ rms: 1, zcr: 0 }));
  const loud = (n: number): SpotterFrame[] =>
    Array.from({ length: n }, () => ({ rms: 30, zcr: 0.05 }));

  it("finds nothing in room tone", () => {
    expect(segmentSpeech(quiet(200))).toEqual([]);
  });

  it("ends an utterance on trailing silence, not on a fixed window", () => {
    const [segment, ...rest] = segmentSpeech([...quiet(10), ...loud(15), ...quiet(60)]);
    expect(rest).toEqual([]);
    expect(segment.complete).toBe(true);
    expect(segment.end - segment.start).toBe(15);
  });

  it("keeps a short internal pause inside one utterance", () => {
    // 200 ms between syllables is a /d/, not the end of the sentence.
    const segments = segmentSpeech([...quiet(10), ...loud(8), ...quiet(10), ...loud(8), ...quiet(60)]);
    expect(segments).toHaveLength(1);
  });

  it("splits two utterances separated by a real pause", () => {
    expect(segmentSpeech([...quiet(10), ...loud(8), ...quiet(40), ...loud(8), ...quiet(40)])).toHaveLength(2);
  });

  it("caps a segment that never ends", () => {
    const segments = segmentSpeech(loud(2_000), { maxSegmentMs: 1_000 });
    expect(segments.length).toBeGreaterThan(1);
    expect(segments[0].complete).toBe(false);
    expect((segments[0].end - segments[0].start) * SPOTTER_FRAME_MS).toBeLessThanOrEqual(1_000);
  });
});

describe("wakeShape", () => {
  it("refuses an empty window rather than scoring it", () => {
    const shape = wakeShape([]);
    expect(shape.wakeShaped).toBe(false);
    expect(shape.reason).toBe("no speech in the window");
  });

  it("names why it said no", () => {
    // Via the spotter, so the window starts at the word rather than at the
    // room tone in front of it — the same framing the ear gives it at runtime.
    expect(spot("n_yes_sam").utterances[0].reason).toBe("one syllable, Ada has two");
    expect(spot("n_number_sam").utterances[0].reason).toBe(
      "too many syllables to start with Ada"
    );
    expect(spot("n_uh_sam").utterances[0].reason).toBe("a hard consonant Ada does not have");
  });
});

describe("the spotter on measured speech", () => {
  // The property the whole design rests on: an empty room produces no
  // utterance at all, so nothing can be sent and nothing can be hallucinated
  // over. This is the one that must never regress.
  it.each(["n_silence", "n_silence_loud"])("emits no utterance for %s", (clip) => {
    const spotter = createWakeSpotter({
      onUtterance: () => {
        throw new Error("room tone produced an utterance");
      },
    });
    for (const frame of frames(clip)) spotter.push(frame);
    expect(spotter.speaking).toBe(false);
  });

  it("accepts 8 of the 9 clips that contain the wake word", () => {
    const accepted = POSITIVES.filter((name) => spot(name).accepted);
    // Daniel's bare "Ada" is 140 ms with the two syllables run together; it is
    // the known miss, recorded rather than hidden.
    expect(POSITIVES.filter((n) => !accepted.includes(n))).toEqual(["p_ada_dan"]);
    expect(accepted).toHaveLength(8);
  });

  it("rejects 10 of the 17 clips that do not", () => {
    const accepted = NEGATIVES.filter((name) => spot(name).accepted);
    // These are the measured false accepts. Each costs exactly one transcript,
    // which then does not match the wake word, so none of them can wake Ada.
    expect(accepted.sort()).toEqual([
      "n_adapter_sam",
      "n_canada_sam",
      "n_cap_sam",
      "n_coffee_alex",
      "n_okay_sam",
      "n_thanks_sam",
      "n_whatever_alex",
    ]);
  });

  it("still rejects the sentences that most look like a run request", () => {
    for (const clip of ["n_router_sam", "n_monday_dan", "n_number_sam"]) {
      expect(spot(clip).accepted).toBe(false);
    }
  });

  it("passes a wake word with the command behind it, judging only the lead", () => {
    const { accepted, utterances } = spot("p_adacmd_sam");
    expect(accepted).toBe(true);
    // The clip is ~3 s long; the shape test read only the leading window.
    expect(utterances[0].durationMs).toBeLessThanOrEqual(620);
  });
});

describe("createWakeSpotter", () => {
  it("announces speech before it knows the shape, so recording can start", () => {
    const events: string[] = [];
    const spotter = createWakeSpotter({
      onSpeechStart: () => events.push("start"),
      onUtterance: ({ shape }) => events.push(shape.wakeShaped ? "hit" : "miss"),
    });
    for (const frame of frames("p_ada_sam")) {
      spotter.push(frame);
      if (events.length === 1) expect(spotter.speaking).toBe(true);
    }
    expect(events).toEqual(["start", "hit"]);
  });

  it("forgets an open utterance on reset", () => {
    const spotter = createWakeSpotter({
      onUtterance: () => {
        throw new Error("emitted after reset");
      },
    });
    for (const frame of frames("p_ada_sam").slice(0, 25)) spotter.push(frame);
    spotter.reset();
    expect(spotter.speaking).toBe(false);
  });
});

describe("createAnalyserFeed", () => {
  class FakeAnalyser {
    fftSize = 1024;
    private cursor = 0;
    constructor(private readonly script: number[][]) {}
    getByteTimeDomainData(out: Uint8Array) {
      const frame = this.script[Math.min(this.cursor, this.script.length - 1)];
      this.cursor += 1;
      for (let i = 0; i < out.length; i += 1) out[i] = frame[i % frame.length];
    }
  }

  function fakeContext(script: number[][], onClose: () => void) {
    const analyser = new FakeAnalyser(script);
    return class {
      createAnalyser() {
        return analyser as unknown as AnalyserNode;
      }
      createMediaStreamSource() {
        return { connect: () => undefined } as unknown as MediaStreamAudioSourceNode;
      }
      close() {
        onClose();
        return Promise.resolve();
      }
    } as unknown as typeof AudioContext;
  }

  /** A loud square frame and a silent one, on the byte time-domain scale. */
  const LOUD = [108, 108, 108, 108, 148, 148, 148, 148];
  const QUIET = [128, 128, 128, 128, 128, 128, 128, 128];

  it("turns analyser reads into spotter frames", () => {
    const seen: SpotterFrame[] = [];
    let tick: (() => void) | null = null;
    const feed = createAnalyserFeed(
      {
        stream: {} as MediaStream,
        AudioContext: fakeContext([LOUD, QUIET], () => undefined),
        setInterval: ((fn: () => void) => {
          tick = fn;
          return 1;
        }) as unknown as typeof globalThis.setInterval,
        clearInterval: (() => undefined) as unknown as typeof globalThis.clearInterval,
      },
      { push: (f) => seen.push(f), reset: () => undefined, speaking: false }
    );
    tick!();
    tick!();
    expect(seen).toHaveLength(2);
    expect(seen[0].rms).toBeCloseTo(20, 5);
    expect(seen[1]).toEqual({ rms: 0, zcr: 0 });
    feed.close();
  });

  it("stops feeding and resets the spotter on close", () => {
    let ticks = 0;
    let reset = 0;
    let closed = 0;
    let cleared = 0;
    let tick: (() => void) | null = null;
    const feed = createAnalyserFeed(
      {
        stream: {} as MediaStream,
        AudioContext: fakeContext([LOUD], () => {
          closed += 1;
        }),
        setInterval: ((fn: () => void) => {
          tick = fn;
          return 7;
        }) as unknown as typeof globalThis.setInterval,
        clearInterval: ((id: number) => {
          expect(id).toBe(7);
          cleared += 1;
        }) as unknown as typeof globalThis.clearInterval,
      },
      {
        push: () => {
          ticks += 1;
        },
        reset: () => {
          reset += 1;
        },
        speaking: false,
      }
    );
    tick!();
    feed.close();
    feed.close();
    expect([ticks, reset, closed, cleared]).toEqual([1, 2, 2, 1]);
  });

  it("refuses rather than degrading when the audio graph will not build", () => {
    const Broken = class {
      createAnalyser(): AnalyserNode {
        throw new Error("no analyser here");
      }
      close() {
        return Promise.resolve();
      }
    } as unknown as typeof AudioContext;
    expect(() =>
      createAnalyserFeed({ stream: {} as MediaStream, AudioContext: Broken }, {
        push: () => undefined,
        reset: () => undefined,
        speaking: false,
      })
    ).toThrow(/could not measure the room level: no analyser here/);
  });
});
