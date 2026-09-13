// The spoken digest of a finished run — text composed for the EAR.
//
// A screen can show "10.6 × 9.7 mm" and let the eye parse the glyphs; a voice
// has to say "10.6 by 9.7 millimeters" or it reads as a fax machine. So this
// module writes its own sentences from the response fields and deliberately
// copies no display string from anywhere else in the app: the visual summary
// and the spoken one are different registers of the same facts, not one
// string reused.
//
// Pure function, no DOM, no storage — that is what makes it unit-testable and
// what keeps "what gets said" reviewable in one place.

import type { Finding, RunResult } from "@/lib/silkscreen/types";

/** Speak at most this many findings before summarising the rest. */
export const MAX_SPOKEN_FINDINGS = 3;

/**
 * The engine's severity vocabulary, worst first, so the three findings that
 * get airtime are the three that matter. Unknown severities sort last rather
 * than vanishing — the reviewer may grow vocabulary this build has not heard.
 */
const SEVERITY_RANK: string[] = [
  "blocker",
  "error",
  "marginal",
  "warning",
  "note",
  "info",
];

function severityRank(severity: string): number {
  const index = SEVERITY_RANK.indexOf(severity);
  return index === -1 ? SEVERITY_RANK.length : index;
}

/** "10.6" not "10.60", "12" not "12.0" — trailing zeros are noise out loud. */
function spokenNumber(value: number): string {
  const rounded = Math.round(value * 10) / 10;
  return Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1);
}

/** A sentence ends in one period; a title that brought its own keeps it. */
function sentence(text: string): string {
  const trimmed = text.trim().replace(/[.\s]+$/, "");
  return trimmed ? `${trimmed}.` : "";
}

function boardSentence(result: RunResult): string {
  const board = result.board_mm ?? result.placements?.board_mm;
  const status = (result.status ?? "").trim().toLowerCase();
  const size = board
    ? `The board is ${spokenNumber(board[0])} by ${spokenNumber(board[1])} millimeters`
    : "";
  const solved =
    status === "optimal"
      ? "placed optimally"
      : status === "feasible"
        ? "with a feasible placement"
        : status === "fallback"
          ? "placed by the fallback packer"
          : "";
  if (size && solved) return `${size}, ${solved}.`;
  if (size) return `${size}.`;
  if (solved) return sentence(`The placement finished ${status}`);
  return "The run finished.";
}

function findingSentence(finding: Finding): string {
  const severity = String(finding.severity ?? "").trim();
  const title = (finding.title ?? "").trim();
  const detail = (finding.detail ?? "").trim();
  const body = [title, detail]
    .filter(Boolean)
    .map(sentence)
    .join(" ");
  if (!body) return "";
  if (!severity) return body;
  // "Blocker: VOUT has no bulk capacitor." — the severity is the headline.
  return `${severity.charAt(0).toUpperCase()}${severity.slice(1)}: ${body}`;
}

/**
 * One short spoken digest of a run result.
 *
 * The shape: one sentence of board size and solver status, then the worst
 * findings — at most `MAX_SPOKEN_FINDINGS` of them — then a count of what was
 * left unsaid. An absent review and an empty review get different sentences,
 * because "nothing was checked" and "checks ran and found nothing" are
 * different claims and the voice must not blur them.
 */
export function summarizeRun(result: RunResult): string {
  const parts: string[] = [boardSentence(result)];

  const findings = result.findings;
  // The engine's `review` block outranks the finding list: a critic that was
  // asked and answered nothing readable leaves an empty list too, and
  // "found nothing to flag" over that is the clean-board reading the block
  // exists to refuse. An older engine sends no block and its list is read
  // as before.
  const review = result.review;
  if (review && review.status === "failed") {
    parts.push("The review failed, so nothing is known about this board.");
  } else if (review && review.status === "skipped") {
    parts.push("This run carried no review.");
  } else if (findings === undefined) {
    parts.push("This run carried no review.");
  } else if (findings.length === 0) {
    parts.push("The review found nothing to flag.");
  } else {
    const ordered = [...findings].sort(
      (a, b) =>
        severityRank(String(a.severity ?? "")) -
        severityRank(String(b.severity ?? ""))
    );
    const spoken = ordered
      .slice(0, MAX_SPOKEN_FINDINGS)
      .map(findingSentence)
      .filter(Boolean);
    parts.push(...spoken);
    const rest = findings.length - MAX_SPOKEN_FINDINGS;
    if (rest > 0) {
      parts.push(
        rest === 1
          ? "And 1 more finding in the review."
          : `And ${rest} more findings in the review.`
      );
    }
  }

  return parts.filter(Boolean).join(" ");
}
