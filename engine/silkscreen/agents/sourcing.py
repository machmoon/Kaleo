"""Look the parts up, and check what the model claims before repeating it.

The :mod:`silkscreen.agents.propose` loop applied to the bill of materials:
the model is shown the rows :func:`silkscreen.sourcing.bom_rows` derived
from the board -- ref, kind, value, package -- and asked for a manufacturer,
a part number, and a datasheet URL per row. Its answer goes through
:func:`silkscreen.sourcing.parse_sourcing_response`, every failure batched
back as one repair prompt, and after the budget the loop gives up loudly
with a warning and the rows unchanged rather than shipping a guess.

What comes back is then **checked, not trusted, to the extent it can be**.
A part number is put to a distributor when one is configured
(:mod:`.distributor`: Mouser, behind ``MOUSER_API_KEY``) and becomes
``"verified"`` only on an exact listing; otherwise it stays ``"proposed"``
with the reason on the row. A datasheet URL can always be checked:
:func:`probe_pdf` fetches the first eight bytes and reports ``"verified"``
only when they read ``%PDF-``, because distributors serve HTML viewer
pages from ``.pdf`` links (the :mod:`~silkscreen.agents.datasheet` lesson)
and a link that opens a web page is not a datasheet the engineer can file.
The probe is a ``probe=`` seam so the suite stays offline; the URL goes
through the same SSRF guard :func:`~silkscreen.agents.grounding.fetch_pdf`
applies, since a model that can name any URL can name the metadata
service. Probes run a few at a time under one wall-clock budget for the
whole board, because the order step waits on this stage and a handful of
slow manufacturer hosts must not turn that wait into minutes; a URL the
budget never reached is ``"unprobed"``, said out loud, never ``"none"``.

The prompt says a null beats an invention, and the parser makes null cheap
to say: every field but ``ref`` may be null, and an empty string is read as
null rather than as a part number of zero characters.

Intended live tier: :data:`~silkscreen.agents.model.CHEAP_MODEL` -- naming a
common part is a recall pass, not a reasoning one. The tier is the caller's
to construct; this module only ever sees the :class:`Model` protocol.
"""

from __future__ import annotations

import dataclasses
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import Any
from urllib.parse import urljoin

from ..sourcing import (
    DATASHEET_STATUSES,
    SourcingEntry,
    SourcingResult,
    SourcingValidationError,
    parse_sourcing_response,
)
from .distributor import Verify, from_env, verify_mpns
from .grounding import (
    _MAX_REDIRECTS,
    _REDIRECT_CODES,
    _USER_AGENT,
    _NoRedirect,
    _validate_url,
)
from .model import Model

__all__ = [
    "DEFAULT_PROBE_BUDGET_S",
    "MAX_CONCURRENT_PROBES",
    "SOURCING_MARKER",
    "SOURCING_PROMPT",
    "probe_datasheets",
    "probe_pdf",
    "propose_sourcing",
]

#: Frozen (the sourcing contract): appears verbatim in every prompt so any
#: ``ScriptedModel.by_marker`` can key on it.
SOURCING_MARKER = "SOURCING-BOM v1"

#: What a PDF starts with. The probe reads exactly this many bytes.
_PDF_MAGIC = b"%PDF-"

#: Wall-clock seconds for probing every datasheet URL on one board. Sized
#: for the order step, which waits on this: a whole board's probes fit in
#: the time one slow host would otherwise take on its own.
DEFAULT_PROBE_BUDGET_S = 20.0

#: Probes in flight at once -- :data:`~.stages.MAX_CONCURRENT_READS`'s
#: figure, for the same reason (a handful of outbound connections, not a
#: thread per part).
MAX_CONCURRENT_PROBES = 4

#: How much of the batched error list a give-up warning carries.
_WARNING_CHARS = 400

SOURCING_PROMPT = f"""\
You are a junior hardware engineer sourcing the parts on a finished PCB
({SOURCING_MARKER}). For each part listed below, name a real, currently
manufactured part that matches its value AND its package. Respond with ONE
JSON object -- no prose, no code fence:

{{
  "parts": [
    {{"ref": "<ref exactly as listed>",
     "manufacturer": "<manufacturer name>" | null,
     "mpn": "<manufacturer part number>" | null,
     "datasheet_url": "<https URL of the manufacturer's PDF datasheet>" | null,
     "note": "<one short sentence the engineer should know>" | null}}
  ]
}}

Hard rules -- an answer breaking any of these is rejected automatically:

1. One entry per ref, every ref covered, no ref you were not given.
2. null beats invention. If you are not sure a part number exists, say null.
   A wrong MPN gets ordered; a null gets looked up by a person.
3. The package must match the land pattern named for the part (a 0603
   resistor is not a 0805 resistor). If nothing matches, say null.
4. A datasheet_url must be the direct link to a PDF on the manufacturer's
   own site, or null. Never a distributor page, never a search result. The
   URL will be fetched and checked; a link to an HTML page is reported.
5. Part numbers are at most 64 characters. Notes are short.
"""


def _rows_block(rows: Sequence[SourcingEntry]) -> str:
    """The board's rows, rendered for the prompt. Deterministic text."""
    lines = []
    for row in rows:
        value = row.value if row.value else "(no value)"
        lines.append(
            f"  - {row.ref}: kind={row.kind} value={value} package={row.package}"
        )
    return "\n".join(lines)


def probe_pdf(url: str, *, timeout_s: float = 10.0) -> str:
    """Is there a PDF at ``url``? A ``datasheet_status``, never an exception.

    The URL is validated with the grounding SSRF guard first -- scheme,
    then every address the host resolves to must be global -- and again on
    every redirect, since a public host can redirect to a private one. The
    body is read only as far as the PDF magic (five bytes) and the
    connection closed; a datasheet can be tens of megabytes and the question
    is only what kind of file it is.

    ``"verified"`` means those bytes read ``%PDF-``; ``"not_pdf"`` means the
    server answered with something else (an HTML viewer page, usually);
    ``"unreachable"`` covers everything that stopped the probe -- a rejected
    URL, a DNS failure, a refused connection, a timeout, a non-2xx status,
    too many redirects. All of those are one answer on purpose: none of them
    says whether a datasheet exists, so none of them may be reported as
    anything but unverified.
    """
    opener = urllib.request.build_opener(_NoRedirect)
    current = url
    try:
        for _ in range(_MAX_REDIRECTS + 1):
            _validate_url(current)
            request = urllib.request.Request(
                current, headers={"User-Agent": _USER_AGENT}
            )
            try:
                response = opener.open(request, timeout=timeout_s)
            except urllib.error.HTTPError as exc:
                location = exc.headers.get("Location") if exc.headers else None
                code = exc.code
                exc.close()
                if code in _REDIRECT_CODES and location:
                    current = urljoin(current, location)
                    continue
                return "unreachable"
            with response:
                head = response.read(len(_PDF_MAGIC))
            return "verified" if head.startswith(_PDF_MAGIC) else "not_pdf"
    except Exception:  # noqa: BLE001 -- every failure is the one answer
        return "unreachable"
    return "unreachable"


def probe_datasheets(
    urls: Sequence[str],
    probe: Callable[[str], str],
    *,
    budget_s: float = DEFAULT_PROBE_BUDGET_S,
    workers: int = MAX_CONCURRENT_PROBES,
) -> tuple[dict[str, str], list[str]]:
    """Probe every distinct URL, at most ``workers`` at a time, and stop
    waiting after ``budget_s``.

    Returns ``url -> datasheet_status`` for every URL asked -- ``"unprobed"``
    for the ones the budget ran out on -- and the warnings (one, naming the
    budget and the count, when any were left). A probe that raises is not
    swallowed: the datasheet probe never raises by contract, so an exception
    here is a programming error and propagates, as does a status outside
    :data:`~silkscreen.sourcing.DATASHEET_STATUSES`.

    Probes still in flight when the budget ends are abandoned, not killed
    (a socket read cannot be interrupted); their own timeouts end them.
    """
    distinct = list(dict.fromkeys(urls))
    if not distinct:
        return {}, []
    statuses: dict[str, str] = {}
    pool = ThreadPoolExecutor(
        max_workers=max(1, min(workers, len(distinct))),
        thread_name_prefix="silkscreen-probe",
    )
    try:
        futures: dict[Future[str], str] = {
            pool.submit(probe, url): url for url in distinct
        }
        done, pending = wait(futures, timeout=max(0.0, budget_s))
        for future in done:
            url = futures[future]
            status = future.result()
            if status not in DATASHEET_STATUSES:
                raise ValueError(
                    f"the datasheet probe answered {status!r} for {url}; "
                    f"expected one of {sorted(DATASHEET_STATUSES)}"
                )
            statuses[url] = status
        for future in pending:
            future.cancel()
            statuses[futures[future]] = "unprobed"
    finally:
        pool.shutdown(wait=False)
    warnings: list[str] = []
    if pending:
        warnings.append(
            f"datasheet probes stopped after {budget_s:g} s: "
            f"{len(pending)} of {len(distinct)} URL(s) left unprobed"
        )
    return statuses, warnings


def _apply(
    row: SourcingEntry,
    proposal: dict[str, str | None],
    statuses: dict[str, str],
) -> SourcingEntry:
    """A row with the model's proposal on it and its probed status."""
    url = proposal["datasheet_url"]
    status = "none" if url is None else statuses[url]
    mpn = proposal["mpn"]
    return dataclasses.replace(
        row,
        manufacturer=proposal["manufacturer"],
        mpn=mpn,
        mpn_status="proposed" if mpn is not None else "none",
        datasheet_url=url,
        datasheet_status=status,
        note=proposal["note"],
    )


def propose_sourcing(
    model: Model,
    rows: Sequence[SourcingEntry],
    *,
    probe: Callable[[str], str] | None = None,
    budget_s: float = DEFAULT_PROBE_BUDGET_S,
    verify: Verify | None = None,
    max_repairs: int = 1,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> SourcingResult:
    """Ask the model to source ``rows``; return every row with its statuses.

    One model call, plus at most ``max_repairs`` more when the answer fails
    :func:`~silkscreen.sourcing.parse_sourcing_response` -- the batched
    errors go back as a single repair prompt each time. When the budget is
    spent the rows come back **unchanged** (every status ``"none"``) with one
    warning saying so: an unsourced BOM is honest, a half-parsed one is not.

    Every proposed datasheet URL is put through ``probe`` (default
    :func:`probe_pdf`; tests pass a fake) by :func:`probe_datasheets`, a
    few at a time and for at most ``budget_s`` seconds in all; the answer
    becomes the row's ``datasheet_status``, ``"unprobed"`` for the URLs the
    budget never reached. A probe answering outside the frozen vocabulary
    is a programming error and raises ``ValueError``.

    Every proposed part number is then put to ``verify`` (default:
    :func:`~.distributor.from_env`, a Mouser client when ``MOUSER_API_KEY``
    is set and nothing otherwise) through
    :func:`~.distributor.verify_mpns`; with no verifier the rows stay
    ``"proposed"``.

    ``on_event`` receives ``sourcing.round`` per rejected answer and one
    ``sourcing.part`` per row -- ref and the two statuses, never the model's
    text -- in board order.

    An empty ``rows`` returns an empty result without calling the model.

    Raises:
        ModelError: the model could not be reached. Deliberately not
            wrapped -- an outage is a different condition from a bad answer,
            and callers route them differently (the
            :func:`~silkscreen.agents.propose.propose_circuit` convention).
    """
    rows = list(rows)
    if not rows:
        return SourcingResult([])
    if probe is None:
        probe = probe_pdf
    if verify is None:
        verify = from_env()
    refs = [row.ref for row in rows]
    table = _rows_block(rows)
    prompt = f"{SOURCING_PROMPT}\nParts on the board:\n{table}\n"

    proposals: dict[str, dict[str, str | None]] | None = None
    last_errors: list[str] = []
    for round_no in range(max_repairs + 1):
        # A transport failure is NOT wrapped: ModelError propagates so a
        # FallbackModel's failover -- and the service's 502 -- stay intact.
        raw = model.generate(prompt, temperature=0.0, max_output_tokens=4096)
        try:
            proposals = parse_sourcing_response(raw, refs)
        except SourcingValidationError as exc:
            last_errors = list(exc.errors)
        else:
            break
        if on_event is not None:
            on_event(
                {
                    "event": "sourcing.round",
                    "round": round_no + 1,
                    "errors": len(last_errors),
                    "first_error": str(last_errors[0])[:160] if last_errors else "",
                }
            )
        if round_no == max_repairs:
            break
        problems = "\n".join(f"  - {e}" for e in last_errors)
        prompt = (
            f"{SOURCING_PROMPT}\nParts on the board:\n{table}\n\n"
            f"Your previous answer was rejected. Fix ALL of these and return "
            f"the corrected JSON object:\n{problems}\n\n"
            f"Your previous answer was:\n{raw}\n"
        )

    warnings: list[str] = []
    if proposals is None:
        detail = "; ".join(last_errors)[:_WARNING_CHARS]
        warnings.append(
            f"parts were not sourced: no usable answer after "
            f"{max_repairs + 1} attempt(s) ({detail})"
        )
        proposals = {}

    urls = [
        p["datasheet_url"]
        for p in proposals.values()
        if p["datasheet_url"] is not None
    ]
    statuses, probe_warnings = probe_datasheets(urls, probe, budget_s=budget_s)
    warnings.extend(probe_warnings)

    entries: list[SourcingEntry] = []
    for row in rows:
        proposal = proposals.get(row.ref)
        entries.append(row if proposal is None else _apply(row, proposal, statuses))
    if verify is not None:
        entries, verify_warnings = verify_mpns(entries, verify)
        warnings.extend(verify_warnings)

    for entry in entries:
        if on_event is not None:
            on_event(
                {
                    "event": "sourcing.part",
                    "ref": entry.ref,
                    "mpn_status": entry.mpn_status,
                    "datasheet_status": entry.datasheet_status,
                }
            )
    return SourcingResult(entries, warnings=warnings)
