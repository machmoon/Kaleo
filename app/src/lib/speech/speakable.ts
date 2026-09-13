// Display text in, spoken text out.
//
// This product's vocabulary is hostile to a speech synthesiser. A hardware
// engineer reads `0_device_pin_7`, `SOT-223`, `10uF` and `Edge.Cuts` off a
// screen without noticing; a synthesiser reads them as a mangled token, an
// unpredictable number, "ten uff", and — worst of the four — a *sentence
// boundary*, because the dot in `Edge.Cuts` is a full stop as far as prosody
// is concerned. That last one is a real part of the "it sounds chopped"
// complaint: the voice pauses in the middle of a layer name.
//
// So this module exists, and it is deliberately the least powerful thing that
// fixes it: **one pure function, no I/O, no dependency, no model call.** It is
// wired in at exactly one place (`announce`, the single gate in front of the
// mouth), so nothing in the app can bypass it, and it changes *only* what is
// spoken. The strip still shows `C3` and `Edge.Cuts`; the ear gets "see three"
// and "edge cuts". That display/speech split is the whole design.
//
// Two rules the implementation is written against:
//
//  - **Never invent a fact.** Every rule is a pronunciation change, not a
//    rewording. "Blocker: VOUT has no bulk capacitor" becomes "Blocker: V out
//    has no bulk capacitor" — same claim, said correctly. A rule that dropped
//    or summarised a token would make the voice disagree with the screen,
//    which is the one thing a spoken digest must never do.
//  - **A rule that does not fire leaves the token alone.** Unknown vocabulary
//    is passed through verbatim rather than guessed at, because a wrong
//    expansion is worse than a slightly awkward one: the listener can decode
//    an awkward reading and cannot decode an invented word.
//
// Replacements are protected as they are made (see `transform`), so a later,
// more general rule can never chew on an earlier rule's output — the
// "front copper" that `F.Cu` produced is not re-examined by the underscore
// rule, and `S O T two twenty three` is not re-examined by anything.
//
// Where engineers genuinely disagree on how something is said, the choice is
// marked DEFAULT in a comment. Those are the lines to change first if someone
// with better ears than mine objects.

// ------------------------------------------------------------------ numbers

const ONES = [
  "zero",
  "one",
  "two",
  "three",
  "four",
  "five",
  "six",
  "seven",
  "eight",
  "nine",
  "ten",
  "eleven",
  "twelve",
  "thirteen",
  "fourteen",
  "fifteen",
  "sixteen",
  "seventeen",
  "eighteen",
  "nineteen",
];

const TENS = [
  "",
  "",
  "twenty",
  "thirty",
  "forty",
  "fifty",
  "sixty",
  "seventy",
  "eighty",
  "ninety",
];

/** 0–999999 as ordinary English. Anything else comes back as its own digits. */
export function numberWords(n: number): string {
  if (!Number.isFinite(n) || n < 0 || n > 999999 || !Number.isInteger(n)) {
    return String(n);
  }
  if (n < 20) return ONES[n];
  if (n < 100) {
    const tens = TENS[Math.floor(n / 10)];
    const rest = n % 10;
    return rest ? `${tens} ${ONES[rest]}` : tens;
  }
  if (n < 1000) {
    const rest = n % 100;
    const head = `${ONES[Math.floor(n / 100)]} hundred`;
    return rest ? `${head} ${numberWords(rest)}` : head;
  }
  const rest = n % 1000;
  const head = `${numberWords(Math.floor(n / 1000))} thousand`;
  return rest ? `${head} ${numberWords(rest)}` : head;
}

/**
 * A decimal said the way a person says it: "three point three", not
 * "three point three zero". The integer part is a number, the fraction is
 * digits — which is how every engineer reads 2.54 aloud.
 */
function decimalWords(text: string): string {
  const [whole, fraction] = text.split(".");
  const head = numberWords(Number(whole));
  if (fraction === undefined) return head;
  const digits = [...fraction].map((d) => ONES[Number(d)]).join(" ");
  return `${head} point ${digits}`;
}

/**
 * The number *inside a part number*, which is not read as a quantity.
 *
 * `SOT-223` is "S O T two twenty three", never "two hundred and twenty
 * three"; `LQFP-144` is "one forty four". Two digits are read as a number
 * ("forty four"), three as digit-then-pair ("two twenty three"), with the
 * two round cases people do say as quantities kept: 100 is "one hundred",
 * and a middle zero becomes "oh" (105 → "one oh five").
 */
export function partNumberWords(digits: string): string {
  const n = Number(digits);
  if (digits.length <= 2) return numberWords(n);
  if (digits.length === 3) {
    const head = ONES[Number(digits[0])];
    const rest = digits.slice(1);
    if (rest === "00") return `${head} hundred`;
    if (rest[0] === "0") return `${head} oh ${ONES[Number(rest[1])]}`;
    return `${head} ${numberWords(Number(rest))}`;
  }
  return [...digits].map((d) => ONES[Number(d)]).join(" ");
}

/** "04" → "oh four", "12" → "twelve". The unit of an imperial chip code. */
function pairWords(pair: string): string {
  if (pair[0] === "0") return `oh ${ONES[Number(pair[1])]}`;
  return numberWords(Number(pair));
}

/**
 * An imperial chip size is read as two pairs, never as a quantity: `0402` is
 * "oh four oh two" and `1206` is "twelve oh six". Nobody has ever said "one
 * thousand two hundred and six" about a resistor.
 */
export function chipSizeWords(code: string): string {
  return `${pairWords(code.slice(0, 2))} ${pairWords(code.slice(2))}`;
}

// -------------------------------------------------------------- vocabulary

/** KiCad is "KEE-cad" (the project says so); a synthesiser guesses "kick-add". */
const KICAD = "Kee Cad";

/** Spell a run of capitals out: "PCB" → "P C B". */
function letters(word: string): string {
  return [...word.toUpperCase()].join(" ");
}

/**
 * Letter *names*, for the one place a bare capital is most likely to be read
 * as a word rather than a letter: a reference designator, where the letter
 * stands alone against a number. "C3" is "see three", never "kuh three".
 *
 * Acronyms and package families keep the spaced-capitals form above, which
 * every synthesiser already reads as letters because the run is longer than
 * one character. This split is deliberate; it is not an oversight.
 */
const LETTER_NAMES: Record<string, string> = {
  A: "ay",
  B: "bee",
  C: "see",
  D: "dee",
  E: "ee",
  F: "eff",
  G: "gee",
  H: "aitch",
  I: "eye",
  J: "jay",
  K: "kay",
  L: "el",
  M: "em",
  N: "en",
  O: "oh",
  P: "pee",
  Q: "cue",
  R: "arr",
  S: "ess",
  T: "tee",
  U: "you",
  V: "vee",
  W: "double you",
  X: "ex",
  Y: "why",
  Z: "zee",
};

/** "SW" → "ess double you". The ref-designator half of `letters`. */
function letterNames(word: string): string {
  return [...word.toUpperCase()].map((c) => LETTER_NAMES[c] ?? c).join(" ");
}

/** KiCad layer names. The dot is the prosody bug; the words are the fix. */
const LAYERS: Record<string, string> = {
  "Edge.Cuts": "edge cuts",
  "F.Cu": "front copper",
  "B.Cu": "back copper",
  "F.CrtYd": "front courtyard",
  "B.CrtYd": "back courtyard",
  "F.SilkS": "front silkscreen",
  "B.SilkS": "back silkscreen",
  "F.Mask": "front solder mask",
  "B.Mask": "back solder mask",
  "F.Paste": "front paste",
  "B.Paste": "back paste",
  "F.Fab": "front fabrication",
  "B.Fab": "back fabrication",
};

/** Net names an engineer says as words rather than as letters. */
const NETS: Record<string, string> = {
  GND: "ground",
  AGND: "analog ground",
  DGND: "digital ground",
  GNDA: "analog ground",
  GNDD: "digital ground",
  PGND: "power ground",
  VBUS: "V bus",
  VIN: "V in",
  VOUT: "V out",
  VREF: "V ref",
  VBAT: "V bat",
  VCC: "V C C",
  VDD: "V D D",
  VSS: "V S S",
  VEE: "V E E",
  AVDD: "A V D D",
  AVSS: "A V S S",
  VDDA: "V D D A",
};

/**
 * Acronyms a synthesiser mispronounces, and how to say them.
 *
 * Only the ones this app actually emits. An acronym that is a real word to
 * an engineer — SPICE, CAD, DIP, STEP, UART, SPICE — is left alone on
 * purpose: a synthesiser already says it right, and spelling it out would
 * make it worse.
 */
const ACRONYMS: Record<string, string> = {
  PCB: letters("PCB"),
  DRC: letters("DRC"),
  ERC: letters("ERC"),
  BOM: letters("BOM"),
  MPN: letters("MPN"),
  SMD: letters("SMD"),
  THT: "through hole",
  HPWL: letters("HPWL"),
  LDO: letters("LDO"),
  // DEFAULT: engineers say both "ell ee dee" and "led". Letters, because the
  // word reading collides with the past tense of "lead" in a sentence.
  LED: letters("LED"),
  ADC: letters("ADC"),
  DAC: letters("DAC"),
  PWM: letters("PWM"),
  SPI: letters("SPI"),
  I2C: "I squared C",
  USB: letters("USB"),
  ESD: letters("ESD"),
  EMI: letters("EMI"),
  RTC: letters("RTC"),
  MCU: letters("MCU"),
  CSV: letters("CSV"),
  STL: letters("STL"),
  GLB: letters("GLB"),
  SVG: letters("SVG"),
  JSON: "jay son",
  API: letters("API"),
  URL: letters("URL"),
  IC: letters("IC"),
};

/** Word expansions inside snake_case machine vocabulary. */
const SNAKE_WORDS: Record<string, string> = {
  lib: "library",
  silk: "silkscreen",
  crtyd: "courtyard",
  fp: "footprint",
  sch: "schematic",
  pcb: letters("PCB"),
  drc: letters("DRC"),
  erc: letters("ERC"),
  bom: letters("BOM"),
  mpn: letters("MPN"),
  hpwl: letters("HPWL"),
  nm: "nanometer",
  mm: "millimeter",
};

/** File extensions, said as what the file *is*. */
const EXTENSIONS: Record<string, string> = {
  kicad_pcb: `the ${KICAD} board file`,
  kicad_sch: `the ${KICAD} schematic file`,
  kicad_pro: `the ${KICAD} project file`,
  kicad_prl: `the ${KICAD} settings file`,
  step: "the step file",
  stl: `the ${letters("STL")} file`,
  glb: `the ${letters("GLB")} file`,
  csv: `the ${letters("CSV")} file`,
  zip: "the zip file",
  json: "the jay son file",
  svg: `the ${letters("SVG")} file`,
  png: `the ${letters("PNG")} file`,
  rpt: "the report file",
  gbr: "the gerber file",
  drl: "the drill file",
};

/**
 * Reference-designator prefixes, from the engine's own vocabulary
 * (`sourcing.bom_rows` reads R/C/L/D/Y off the ref, `board.py` assigns U, J,
 * SW, TP and friends). Restricted to a known list on purpose: a rule that
 * turned any capital-plus-digits into spelled letters would mangle "3V3",
 * "I2C" and a part number that happens to be in the sentence.
 */
const REF_PREFIXES = [
  "SW",
  "TP",
  "FB",
  "RV",
  "JP",
  "MH",
  "BT",
  "R",
  "C",
  "L",
  "D",
  "Q",
  "U",
  "J",
  "Y",
  "X",
  "F",
  "K",
  "T",
];

/** Package families and how their letters are said. */
const PACKAGES: Record<string, string> = {
  SOT: letters("SOT"),
  // DEFAULT: said "SO-ic" by most engineers, but a synthesiser turns that
  // into "soik" or "so-ick" unpredictably, so the letters are the safe
  // reading — never charming, never wrong.
  SOIC: letters("SOIC"),
  SOD: letters("SOD"),
  LQFP: letters("LQFP"),
  TQFP: letters("TQFP"),
  QFP: letters("QFP"),
  QFN: letters("QFN"),
  DFN: letters("DFN"),
  BGA: letters("BGA"),
  MSOP: letters("MSOP"),
  SSOP: letters("SSOP"),
  // DEFAULT: "tee-sop" is common; letters chosen for the same reason as SOIC.
  TSSOP: letters("TSSOP"),
  TO: letters("TO"),
  // DIP is a word to everyone, and a synthesiser already says it.
  DIP: "DIP",
};

/** Unit suffixes, singular and plural. Order matters where one is a prefix. */
const UNITS: [string, string, string][] = [
  // [suffix as written, singular, plural]
  ["µF", "microfarad", "microfarads"],
  ["uF", "microfarad", "microfarads"],
  ["nF", "nanofarad", "nanofarads"],
  ["pF", "picofarad", "picofarads"],
  ["mF", "millifarad", "millifarads"],
  ["µH", "microhenry", "microhenries"],
  ["uH", "microhenry", "microhenries"],
  ["nH", "nanohenry", "nanohenries"],
  ["mH", "millihenry", "millihenries"],
  ["MΩ", "megohm", "megohms"],
  ["kΩ", "kilohm", "kilohms"],
  ["mΩ", "milliohm", "milliohms"],
  ["Ω", "ohm", "ohms"],
  ["MHz", "megahertz", "megahertz"],
  ["kHz", "kilohertz", "kilohertz"],
  ["Hz", "hertz", "hertz"],
  ["mV", "millivolt", "millivolts"],
  ["kV", "kilovolt", "kilovolts"],
  ["µV", "microvolt", "microvolts"],
  ["mA", "milliamp", "milliamps"],
  ["µA", "microamp", "microamps"],
  ["uA", "microamp", "microamps"],
  ["mW", "milliwatt", "milliwatts"],
  ["ms", "millisecond", "milliseconds"],
  ["µs", "microsecond", "microseconds"],
  ["us", "microsecond", "microseconds"],
  ["ns", "nanosecond", "nanoseconds"],
  ["mm", "millimeter", "millimeters"],
  ["µm", "micrometer", "micrometers"],
  ["um", "micrometer", "micrometers"],
  ["cm", "centimeter", "centimeters"],
  ["mil", "mil", "mils"],
  ["V", "volt", "volts"],
  ["A", "amp", "amps"],
  ["W", "watt", "watts"],
  ["F", "farad", "farads"],
  ["H", "henry", "henries"],
];

const UNIT_PATTERN = UNITS.map(([suffix]) =>
  suffix.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
).join("|");

function unitWords(suffix: string, value: string): string {
  const entry =
    UNITS.find(([written]) => written === suffix) ??
    UNITS.find(([written]) => written.toLowerCase() === suffix.toLowerCase());
  if (!entry) return suffix;
  return value === "1" ? entry[1] : entry[2];
}

/** RKM code letter → its unit, for `4k7`, `2M2`, `1R0`. */
const RKM: Record<string, [string, string]> = {
  R: ["ohm", "ohms"],
  r: ["ohm", "ohms"],
  K: ["kilohm", "kilohms"],
  k: ["kilohm", "kilohms"],
  M: ["megohm", "megohms"],
  m: ["milliohm", "milliohms"],
};

// -------------------------------------------------------------- the engine

type Rule = [RegExp, (groups: string[]) => string];

const OPEN = "\u0001";
const CLOSE = "\u0002";

/**
 * Apply the rules in order, protecting each replacement as it is made.
 *
 * Without the protection, the general rules at the bottom of the table would
 * chew on the output of the specific rules at the top — the underscore rule
 * would attack whatever the net rule produced, and the acronym rule would
 * attack "S O T". A protected span is opaque to every later rule and is only
 * unwrapped at the end. Placeholder indices are plain decimals; no rule can
 * match a bare digit run that is not one of the four-digit chip codes, and
 * those never have a leading digit an index can reach.
 */
function transform(text: string, rules: Rule[]): string {
  const kept: string[] = [];
  let out = text;
  for (const [pattern, replace] of rules) {
    out = out.replace(pattern, (...args: unknown[]) => {
      const groups = args.slice(0, args.length - 2) as string[];
      kept.push(replace(groups));
      return `${OPEN}${kept.length - 1}${CLOSE}`;
    });
  }
  return out.replace(
    new RegExp(`${OPEN}(\\d+)${CLOSE}`, "g"),
    (_m, index: string) => kept[Number(index)]
  );
}

const REF_PATTERN = REF_PREFIXES.join("|");
const PACKAGE_PATTERN = Object.keys(PACKAGES).join("|");
const NET_PATTERN = Object.keys(NETS).join("|");
const ACRONYM_PATTERN = Object.keys(ACRONYMS).join("|");
const LAYER_PATTERN = Object.keys(LAYERS)
  .map((name) => name.replace(".", "\\."))
  .join("|");
const EXT_PATTERN = Object.keys(EXTENSIONS).join("|");

const RULES: Rule[] = [
  // --- files and paths, first: they own the slashes and the dots ---------
  // Case-sensitive on purpose: the engine writes lowercase extensions, and a
  // case-insensitive `.step` turns the sentence "It is done.Step two" into a
  // filename.
  [
    new RegExp(`[^\\s"'()]*\\.(${EXT_PATTERN})\\b`, "g"),
    ([match, ext]) => {
      const base = match.split(/[/\\]/).pop() ?? match;
      const stem = base
        .slice(0, base.length - ext.length - 1)
        .replace(/[._-]+/g, " ")
        .trim();
      const said = EXTENSIONS[ext.toLowerCase()] ?? `the ${letters(ext)} file`;
      return stem ? `${stem}, ${said}` : said;
    },
  ],

  // --- KiCad layer names: the dot that fakes a full stop ------------------
  [new RegExp(`\\b(${LAYER_PATTERN})`, "g"), ([match]) => LAYERS[match]],

  // --- packages: SOT-223, LQFP-44, SOIC-8, SOT-23-3 ----------------------
  [
    new RegExp(`\\b(${PACKAGE_PATTERN})-(\\d+)(?:-(\\d+))?\\b`, "g"),
    ([, family, size, pins]) => {
      const head = `${PACKAGES[family]} ${partNumberWords(size)}`;
      return pins ? `${head}, ${numberWords(Number(pins))} pin` : head;
    },
  ],

  // --- imperial chip sizes: 0402 is "oh four oh two", never a quantity ----
  [
    /\b(0201|0402|0603|0805|1206|1210|1008|1812|2010|2512)\b/g,
    ([code]) => chipSizeWords(code),
  ],

  // --- rail names: 3V3, 1V8 ---------------------------------------------
  [
    /\b(\d+)V(\d+)\b/g,
    ([, whole, fraction]) =>
      `${decimalWords(`${whole}.${fraction}`)} volts`,
  ],

  // --- RKM resistor codes: 4k7, 2M2, 1R0 --------------------------------
  [
    /\b(\d+)([RrKkMm])(\d+)\b/g,
    ([, whole, letter, fraction]) => {
      // Always plural: an RKM code always carries a fraction, so even `1R0`
      // is "one point zero ohms".
      const plural = RKM[letter][1];
      const value = `${whole}.${fraction}`;
      return `${decimalWords(value)} ${plural}`;
    },
  ],

  // --- dimensions: 5x5, 10.6 × 9.7 --------------------------------------
  [/(\d)\s?[x×]\s?(?=\d)/g, ([, digit]) => `${digit} by `],

  // --- values with a unit suffix: 10uF, 3.3V, 0.25mm, 16MHz --------------
  [
    // The closing guard is a lookahead, not `\b`: a unit that ends in a
    // non-word character (`kΩ`) has no word boundary after it, and `\b`
    // silently refused to match every ohm value in the app.
    new RegExp(
      `\\b(\\d+(?:\\.\\d+)?)\\s?(${UNIT_PATTERN})(?![A-Za-z0-9_])`,
      "g"
    ),
    ([, value, suffix]) => `${decimalWords(value)} ${unitWords(suffix, value)}`,
  ],

  // --- percentages -------------------------------------------------------
  [/\b(\d+(?:\.\d+)?)\s?%/g, ([, value]) => `${decimalWords(value)} percent`],

  // --- bare resistances: 10k, 4.7k, 100R --------------------------------
  // A value ending in a bare k/M/R is a resistance everywhere this app
  // speaks; nothing else in the vocabulary is written that way.
  [
    /\b(\d+(?:\.\d+)?)([kKMR])\b/g,
    ([, value, letter]) => {
      const [singular, plural] = RKM[letter] ?? RKM.R;
      return `${decimalWords(value)} ${value === "1" ? singular : plural}`;
    },
  ],

  // --- the engine's own net names: 0_device_pin_7 ------------------------
  [
    /\b(\d+)_device_pin_(\d+)\b/g,
    ([, net, pin]) =>
      `net ${numberWords(Number(net))}, device pin ${numberWords(Number(pin))}`,
  ],

  // --- a hierarchical net path: /VBUS, /NET3 ----------------------------
  // The leading slash is a sheet path, not a word. Dropping it is the whole
  // rule; whatever is left goes on to the net and ref rules below.
  [/(^|[\s("'“])\/(?=[A-Za-z])/g, ([, before]) => before],

  // --- named nets --------------------------------------------------------
  [new RegExp(`\\b(${NET_PATTERN})\\b`, "g"), ([name]) => NETS[name]],

  // --- pin-level terminals from the circuit IR: C1.1 ---------------------
  [
    new RegExp(`\\b(${REF_PATTERN})(\\d+)\\.(\\d+)\\b`, "g"),
    ([, prefix, number, pin]) =>
      `${letterNames(prefix)} ${partNumberWords(number)}, pin ${partNumberWords(pin)}`,
  ],

  // --- reference designators: C3, U1, R12 -------------------------------
  [
    new RegExp(`\\b(${REF_PATTERN})(\\d+)\\b`, "g"),
    ([, prefix, number]) => `${letterNames(prefix)} ${partNumberWords(number)}`,
  ],

  // --- DRC/ERC and other snake_case machine vocabulary ------------------
  [
    /\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b/g,
    ([match]) =>
      match
        .split("_")
        .map((word) => SNAKE_WORDS[word] ?? word)
        .join(" "),
  ],

  // --- acronyms ---------------------------------------------------------
  [new RegExp(`\\b(${ACRONYM_PATTERN})\\b`, "g"), ([word]) => ACRONYMS[word]],

  // --- the product's own names ------------------------------------------
  [/\bKiCad\b/g, () => KICAD],

  // --- leftover dotted identifiers: foo.bar reads as two sentences -------
  [/([A-Za-z])\.(?=[a-z])/g, ([, before]) => `${before} `],

  // --- arrows -----------------------------------------------------------
  [/\s*(?:->|→)\s*/g, () => " to "],

  // --- leftover underscores ---------------------------------------------
  [/_/g, () => " "],
];

/**
 * Turn one line of display text into the line that should be said out loud.
 *
 * Pure. Idempotent in the sense that matters: the output contains none of the
 * shapes the rules match, so speaking already-spoken text is a no-op.
 */
export function speakable(text: string): string {
  if (!text) return "";

  // One pre-pass, before the rules: an identifier that joins words with
  // underscores *and* carries a capital (`VCC_3V3`, `SOT-223-3_TabPin2`) is a
  // join of separate things, and the underscore hides each of them from every
  // rule below — `\bVCC\b` does not match inside `VCC_3V3`. Splitting it here
  // rather than in the table is what lets both halves be normalised. All-lower
  // machine vocabulary (`net_conflict`, `0_device_pin_7`) is untouched: it has
  // its own rules and they read the underscores.
  const split = text.replace(
    /\b(?=[A-Za-z0-9]*[A-Z])[A-Za-z0-9]+(?:_[A-Za-z0-9]+)+\b/g,
    (match) => match.replace(/_/g, " ")
  );

  let out = transform(split, RULES);

  // Prosody cleanup. An ellipsis (which `moments.shorten` appends to a
  // truncated engine error) is read as a pause of unpredictable length by
  // some voices and skipped entirely by others; a full stop is what a
  // truncated sentence actually needs. Then make sure sentence punctuation is
  // followed by a space, because "done.It" gets no pause at all.
  out = out
    .replace(/\s*…/g, ".")
    .replace(/\.{3,}/g, ".")
    .replace(/([.!?])(?=[A-Z])/g, "$1 ")
    .replace(/\s+([,.;:!?])/g, "$1")
    .replace(/\s{2,}/g, " ")
    .trim();

  return out;
}
