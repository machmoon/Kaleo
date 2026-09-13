"""The written half: a terminal report, and a self-contained HTML page.

The HTML page is the deliverable -- board on the left, findings on the right,
clicking a finding highlights it on the board and vice versa. It embeds the
SVG inline and carries no external asset, so it opens from a file:// path with
no server and no network.
"""

from __future__ import annotations

import json
from html import escape

from .effort import slider
from .findings import Finding, Origin, Severity
from .render import SEVERITY_COLOUR, render_svg
from .result import AuditResult

__all__ = ["text_report", "html_report", "json_report"]

_MARK = {
    Severity.BLOCKER: "!!",
    Severity.MARGINAL: " !",
    Severity.NOTE: "  ",
}


def text_report(result: AuditResult, *, width: int = 78) -> str:
    lines: list[str] = []
    name = result.source.name if result.source else "board"
    lines.append(f"Review of {name}")
    lines.append(
        f"  thinking:  {slider(result.profile.level)}   "
        f"({result.profile.description})"
    )
    lines.append(f"  checks:    {len(result.rules_run)} deterministic rules ran")
    if result.model_passes:
        for entry in result.model_passes:
            lines.append(f"             model: {entry}")
    elif result.skipped_reason:
        lines.append(f"             model: not run — {result.skipped_reason}")
    lines.append(f"  verdict:   {result.headline()}")
    lines.append("")

    visible = result.visible()
    if not visible:
        lines.append("Nothing to report at this severity floor.")
    for finding in visible:
        badge = "proven" if finding.origin is Origin.PROVEN else "suggested"
        where = f" [{finding.where}]" if finding.where else ""
        lines.append(
            f"{_MARK[finding.severity]} {finding.id} {finding.title}{where}  "
            f"({badge}, {finding.rule})"
        )
        for chunk in _wrap(finding.detail, width - 7):
            lines.append(f"       {chunk}")
        if finding.evidence:
            lines.append(f"       measured: {finding.evidence}")
        for check in finding.checks:
            lines.append(f"       check:    {check}")
        if finding.fix:
            lines.append(f"       fix:      {finding.fix}")
        if not finding.located:
            lines.append("       (not located on the board)")
        lines.append("")

    hidden = len(result.findings) - len(visible)
    if hidden:
        lines.append(f"{hidden} lower-severity finding(s) hidden at this level.")
    return "\n".join(lines).rstrip() + "\n"


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    out: list[str] = []
    line = ""
    for word in words:
        if len(line) + len(word) + 1 > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out


def json_report(result: AuditResult) -> str:
    return json.dumps(result.to_dict(), indent=2) + "\n"


_CSS = """
:root {
  color-scheme: dark;
  --bg: #0b1210; --panel: #121b18; --line: #24332c; --ink: #dfe7ea;
  --muted: #90a49b; --blocker: #ff5a5f; --marginal: #ffb020; --note: #4cc2ff;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
  font: 14px/1.55 ui-monospace, SFMono-Regular, Menlo, monospace; }
header { padding: 20px 24px 14px; border-bottom: 1px solid var(--line); }
h1 { margin: 0 0 6px; font-size: 17px; letter-spacing: .02em; }
.meta { color: var(--muted); font-size: 12.5px; }
.meta b { color: var(--ink); font-weight: 600; }
.slider { font-size: 15px; letter-spacing: .12em; }
main { display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(320px, 1fr);
  gap: 0; align-items: start; }
@media (max-width: 900px) { main { grid-template-columns: 1fr; } }
.board { position: sticky; top: 0; padding: 18px; }
.board svg { width: 100%; height: auto; border: 1px solid var(--line);
  border-radius: 6px; }
.list { padding: 18px 20px 60px; border-left: 1px solid var(--line); }
.f { border: 1px solid var(--line); border-left-width: 4px; border-radius: 5px;
  padding: 10px 12px; margin-bottom: 10px; background: var(--panel);
  cursor: pointer; }
.f:hover, .f.on { border-color: var(--ink); }
.f.blocker { border-left-color: var(--blocker); }
.f.marginal { border-left-color: var(--marginal); }
.f.note { border-left-color: var(--note); }
.f h2 { margin: 0 0 4px; font-size: 13.5px; font-weight: 600; }
.f .id { color: var(--muted); margin-right: 6px; }
.tags { display: flex; gap: 6px; flex-wrap: wrap; margin: 6px 0; }
.tag { font-size: 11px; padding: 1px 7px; border-radius: 999px;
  border: 1px solid var(--line); color: var(--muted); }
.tag.proven { border-color: #2f6b46; color: #7ee2a8; }
.tag.suggested { border-style: dashed; }
.f p { margin: 6px 0 0; color: #c3cfd4; font-size: 12.5px; }
.f .ev { color: var(--muted); font-size: 12px; margin-top: 6px; }
.f .fix { color: #9fe0bb; font-size: 12px; margin-top: 6px; }
.split { margin: 18px 0 8px; color: var(--muted); font-size: 12px;
  text-transform: uppercase; letter-spacing: .1em; }
svg .finding { opacity: .95; }
svg.dim .finding { opacity: .18; }
svg.dim .finding.on { opacity: 1; }
footer { padding: 16px 24px 40px; color: var(--muted); font-size: 12px;
  border-top: 1px solid var(--line); }
"""

_JS = """
const svg = document.querySelector('.board svg');
const cards = [...document.querySelectorAll('.f')];
function focusOn(id) {
  cards.forEach(c => c.classList.toggle('on', c.dataset.finding === id));
  svg.classList.toggle('dim', Boolean(id));
  svg.querySelectorAll('.finding').forEach(g =>
    g.classList.toggle('on', g.dataset.finding === id));
}
cards.forEach(card => {
  card.addEventListener('click', () =>
    focusOn(card.classList.contains('on') ? null : card.dataset.finding));
});
svg.querySelectorAll('.finding').forEach(g => {
  g.style.cursor = 'pointer';
  g.addEventListener('click', () => {
    focusOn(g.dataset.finding);
    const card = cards.find(c => c.dataset.finding === g.dataset.finding);
    if (card) card.scrollIntoView({block: 'center', behavior: 'smooth'});
  });
});
"""


def _card(finding: Finding) -> str:
    origin = finding.origin.value
    tags = [f'<span class="tag {origin}">{origin}</span>',
            f'<span class="tag">{escape(finding.rule)}</span>']
    if finding.where:
        tags.append(f'<span class="tag">{escape(finding.where)}</span>')
    if not finding.located:
        tags.append('<span class="tag">not located</span>')
    checks = "".join(
        f'<p class="ev">check: {escape(c)}</p>' for c in finding.checks
    )
    return (
        f'<article class="f {finding.severity.value}" '
        f'data-finding="{finding.id}">'
        f'<h2><span class="id">{finding.id}</span>{escape(finding.title)}</h2>'
        f'<div class="tags">{"".join(tags)}</div>'
        f"<p>{escape(finding.detail)}</p>"
        + (
            f'<p class="ev">measured: {escape(finding.evidence)}</p>'
            if finding.evidence
            else ""
        )
        + checks
        + (f'<p class="fix">fix: {escape(finding.fix)}</p>' if finding.fix else "")
        + "</article>"
    )


def html_report(result: AuditResult) -> str:
    """A single file: board render, findings, and what was actually checked."""
    svg = render_svg(result, show_legend=False)
    visible = result.visible()
    proven = [f for f in visible if f.origin is Origin.PROVEN]
    suggested = [f for f in visible if f.origin is Origin.SUGGESTED]
    name = result.source.name if result.source else "board"
    counts = result.counts()

    coverage = [f"{len(result.rules_run)} deterministic rules: "
                + ", ".join(result.rules_run)]
    if result.model_passes:
        coverage += [f"model: {p}" for p in result.model_passes]
    elif result.skipped_reason:
        coverage.append(f"model: not run — {result.skipped_reason}")

    body = [
        "<header>",
        f"<h1>Design review — {escape(name)}</h1>",
        f'<div class="meta slider">{escape(slider(result.profile.level))}</div>',
        f'<div class="meta">{escape(result.profile.description)}</div>',
        f'<div class="meta" style="margin-top:8px">'
        f'<b>{counts["blocker"]}</b> blocker · '
        f'<b>{counts["marginal"]}</b> marginal · '
        f'<b>{counts["note"]}</b> note &nbsp;|&nbsp; '
        f'<b>{counts["proven"]}</b> proven by measurement, '
        f'<b>{counts["suggested"]}</b> suggested by the model</div>',
        "</header>",
        "<main>",
        f'<section class="board">{svg}</section>',
        '<section class="list">',
    ]
    if proven:
        body.append('<div class="split">Proven — measured in the board file</div>')
        body += [_card(f) for f in proven]
    if suggested:
        body.append(
            '<div class="split">Suggested — argued by the model, not verified</div>'
        )
        body += [_card(f) for f in suggested]
    if not visible:
        body.append(
            "<p>No findings at this severity floor. That is a clean bill only "
            "for the checks listed below.</p>"
        )
    body.append("</section></main>")
    body.append(
        "<footer><b>What ran:</b><br>"
        + "<br>".join(escape(line) for line in coverage)
        + "</footer>"
    )

    legend_colours = " ".join(
        f"{s.value}={SEVERITY_COLOUR[s]}" for s in SEVERITY_COLOUR
    )
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Design review — {escape(name)}</title>"
        f"<!-- severity colours: {legend_colours} -->"
        f"<style>{_CSS}</style></head><body>"
        + "".join(body)
        + f"<script>{_JS}</script></body></html>\n"
    )
