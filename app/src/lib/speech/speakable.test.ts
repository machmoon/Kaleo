// The rule table, written as data, because here the test IS the
// specification: a reviewer decides whether this feature is right by reading
// the input → spoken pairs, not by reading the regexes. Anything that is not
// in this file is not a promise the module makes.

import { describe, expect, it } from "vitest";

import { chipSizeWords, numberWords, partNumberWords, speakable } from "./speakable";

/** [what the strip shows, what the mouth should say] */
type Pair = [string, string];

const REFERENCE_DESIGNATORS: Pair[] = [
  ["C3", "see three"],
  ["U1", "you one"],
  ["R12", "arr twelve"],
  ["Q1", "cue one"],
  ["D4", "dee four"],
  ["J2", "jay two"],
  ["Y1", "why one"],
  ["SW1", "ess double you one"],
  ["TP7", "tee pee seven"],
  ["R101", "arr one oh one"],
  ["C3 is missing a bulk capacitor.", "see three is missing a bulk capacitor."],
  ["Blocker: C3 and R12 overlap.", "Blocker: see three and arr twelve overlap."],
  // A pin-level terminal from the circuit IR: `C1.1`, never `C1`.
  ["C1.1", "see one, pin one"],
  ["Connect U1.14 to C3.2.", "Connect you one, pin fourteen to see three, pin two."],
  // Not a designator: no digits, or a prefix the engine never assigns.
  ["Check the DC rail.", "Check the DC rail."],
];

const NET_NAMES: Pair[] = [
  ["0_device_pin_7", "net zero, device pin seven"],
  ["0_device_pin_11", "net zero, device pin eleven"],
  ["2_device_pin_1", "net two, device pin one"],
  ["/VBUS", "V bus"],
  ["/GND", "ground"],
  ["GND", "ground"],
  ["AGND", "analog ground"],
  ["GNDD", "digital ground"],
  ["VCC", "V C C"],
  ["VOUT", "V out"],
  ["VIN", "V in"],
  ["VOUT has no bulk capacitor.", "V out has no bulk capacitor."],
  ["VCC_3V3", "V C C three point three volts"],
];

const KICAD_VOCABULARY: Pair[] = [
  // The dot in a layer name is read as a full stop, which is where a chunk of
  // the "it sounds chopped" complaint comes from.
  ["Edge.Cuts", "edge cuts"],
  ["F.CrtYd", "front courtyard"],
  ["B.CrtYd", "back courtyard"],
  ["F.Cu", "front copper"],
  ["B.Cu", "back copper"],
  ["F.SilkS", "front silkscreen"],
  ["F.Mask", "front solder mask"],
  ["The board has no Edge.Cuts outline.", "The board has no edge cuts outline."],
  // DRC/ERC vocabulary, straight out of the reports KiCad writes.
  ["net_conflict", "net conflict"],
  ["lib_symbol_issues", "library symbol issues"],
  ["lib_footprint_issues", "library footprint issues"],
  ["footprint_link_issues", "footprint link issues"],
  ["unconnected", "unconnected"],
  ["courtyards_overlap", "courtyards overlap"],
  ["silk_over_copper", "silkscreen over copper"],
  ["KiCad", "Kee Cad"],
  ["It is open in KiCad.", "It is open in Kee Cad."],
  ["DRC found 2 errors.", "D R C found 2 errors."],
  ["The PCB is ready.", "The P C B is ready."],
];

const PACKAGES: Pair[] = [
  ["SOT-223", "S O T two twenty three"],
  ["SOT-23", "S O T twenty three"],
  ["SOT-23-3", "S O T twenty three, three pin"],
  ["LQFP-44", "L Q F P forty four"],
  ["LQFP-100", "L Q F P one hundred"],
  ["LQFP-144", "L Q F P one forty four"],
  ["SOIC-8", "S O I C eight"],
  ["SOIC-28", "S O I C twenty eight"],
  ["SOD-123", "S O D one twenty three"],
  ["TO-252", "T O two fifty two"],
  ["QFN-32", "Q F N thirty two"],
  // DIP is a word to everyone; a synthesiser already says it right.
  ["DIP-8", "DIP eight"],
];

const CHIP_SIZES: Pair[] = [
  // An imperial chip code is two pairs of digits, never a quantity.
  ["0402", "oh four oh two"],
  ["0603", "oh six oh three"],
  ["0805", "oh eight oh five"],
  ["1206", "twelve oh six"],
  ["1210", "twelve ten"],
  ["0201", "oh two oh one"],
  ["2512", "twenty five twelve"],
  ["A 0402 resistor.", "A oh four oh two resistor."],
];

const VALUES_AND_UNITS: Pair[] = [
  ["10uF", "ten microfarads"],
  ["10µF", "ten microfarads"],
  ["100nF", "one hundred nanofarads"],
  ["4.7uF", "four point seven microfarads"],
  ["1uF", "one microfarad"],
  ["22pF", "twenty two picofarads"],
  ["3.3V", "three point three volts"],
  ["5V", "five volts"],
  ["1V", "one volt"],
  ["3V3", "three point three volts"],
  ["1V8", "one point eight volts"],
  ["0.25mm", "zero point two five millimeters"],
  ["2.54mm", "two point five four millimeters"],
  ["16MHz", "sixteen megahertz"],
  ["20mA", "twenty milliamps"],
  ["10uH", "ten microhenries"],
  ["50%", "fifty percent"],
  // RKM codes: the decimal point is the unit letter.
  ["4k7", "four point seven kilohms"],
  ["2M2", "two point two megohms"],
  ["1R0", "one point zero ohms"],
  // Bare resistances. DEFAULT: a value ending in a naked k/M/R is read as a
  // resistance, because nothing else this app speaks is written that way.
  ["10k", "ten kilohms"],
  ["4.7k", "four point seven kilohms"],
  ["100R", "one hundred ohms"],
  ["10kΩ", "ten kilohms"],
  ["The 10uF cap on 3.3V.", "The ten microfarads cap on three point three volts."],
];

const FILES: Pair[] = [
  ["board.kicad_pcb", "board, the Kee Cad board file"],
  ["board.kicad_sch", "board, the Kee Cad schematic file"],
  ["board.kicad_pro", "board, the Kee Cad project file"],
  ["/tmp/b/board.kicad_pcb", "board, the Kee Cad board file"],
  ["board.placed.kicad_pcb", "board placed, the Kee Cad board file"],
  ["enclosure.step", "enclosure, the step file"],
  ["bom.csv", "bom, the C S V file"],
  ["I wrote board.kicad_pcb.", "I wrote board, the Kee Cad board file."],
];

const PROSODY: Pair[] = [
  // `moments.shorten` appends this to a truncated engine error.
  ["Routing failed…", "Routing failed."],
  ["Done.Next", "Done. Next"],
  ["5x5mm", "5 by five millimeters"],
  ["10.6 × 9.7", "10.6 by 9.7"],
  ["place -> route", "place to route"],
];

const LEFT_ALONE: Pair[] = [
  // The composed English in `moments.ts` and `summarize.ts` must survive
  // untouched: this module is a pronunciation pass, not a rewriter.
  ["Placement is done. It is open in KiCad.", "Placement is done. It is open in Kee Cad."],
  ["The review found nothing to flag.", "The review found nothing to flag."],
  ["The board is 10.6 by 9.7 millimeters.", "The board is 10.6 by 9.7 millimeters."],
  ["Every stage has run. Nothing was ordered.", "Every stage has run. Nothing was ordered."],
  // The wake word is deliberately not touched: "Ada" is already said right,
  // and the fragility the team measured is in recognition, not synthesis.
  ["Ada, place it.", "Ada, place it."],
  ["", ""],
];

const TABLES: [string, Pair[]][] = [
  ["reference designators", REFERENCE_DESIGNATORS],
  ["net names", NET_NAMES],
  ["KiCad and DRC vocabulary", KICAD_VOCABULARY],
  ["packages", PACKAGES],
  ["chip sizes", CHIP_SIZES],
  ["values and units", VALUES_AND_UNITS],
  ["files and paths", FILES],
  ["prosody", PROSODY],
  ["text left alone", LEFT_ALONE],
];

describe("speakable", () => {
  for (const [name, pairs] of TABLES) {
    describe(name, () => {
      for (const [shown, spoken] of pairs) {
        it(`says ${JSON.stringify(shown)} as ${JSON.stringify(spoken)}`, () => {
          expect(speakable(shown)).toBe(spoken);
        });
      }
    });
  }

  it("is idempotent: speaking already-spoken text changes nothing", () => {
    for (const [, pairs] of TABLES) {
      for (const [shown] of pairs) {
        const once = speakable(shown);
        expect(speakable(once)).toBe(once);
      }
    }
  });

  it("never leaves a protection marker in the output", () => {
    for (const [, pairs] of TABLES) {
      for (const [shown] of pairs) {
        expect(speakable(shown)).not.toMatch(/[\u0001\u0002]/);
      }
    }
  });

  it("does not mutate its input", () => {
    const original = "C3 on /VBUS, 10uF, Edge.Cuts";
    const copy = original.slice();
    speakable(original);
    expect(original).toBe(copy);
  });
});

describe("number helpers", () => {
  const NUMBERS: [number, string][] = [
    [0, "zero"],
    [7, "seven"],
    [12, "twelve"],
    [44, "forty four"],
    [100, "one hundred"],
    [223, "two hundred twenty three"],
  ];
  for (const [n, words] of NUMBERS) {
    it(`counts ${n} as ${words}`, () => expect(numberWords(n)).toBe(words));
  }

  const PART_NUMBERS: [string, string][] = [
    ["8", "eight"],
    ["44", "forty four"],
    ["100", "one hundred"],
    ["105", "one oh five"],
    ["144", "one forty four"],
    ["223", "two twenty three"],
  ];
  for (const [digits, words] of PART_NUMBERS) {
    it(`says part number ${digits} as ${words}`, () =>
      expect(partNumberWords(digits)).toBe(words));
  }

  it("reads a chip code as two pairs", () => {
    expect(chipSizeWords("0603")).toBe("oh six oh three");
    expect(chipSizeWords("1210")).toBe("twelve ten");
  });
});
