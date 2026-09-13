"""Executing a parsed command against the pipeline, and posting the result.

This is the only module that knows both Slack and silkscreen. It calls
:func:`silkscreen.agents.generate_pcb` exactly as the CLI and the service do --
no engine behaviour is reimplemented or reached around -- and turns what comes
back into messages, an image, and file uploads in one thread.

Runs are threaded under the triggering message on purpose. A hardware team's
channel is a shared workspace: the board someone asked for at 11am should still
be readable, with its review attached, when a second person opens the channel at
3pm. DMs would lose that, so nothing here posts to one.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

from silkscreen.agents import ModelError, generate_pcb  # noqa: E402
from silkscreen.agents.model import GeminiModel, Model  # noqa: E402
from silkscreen.agents.review import (  # noqa: E402
    ReviewError,
    ReviewReport,
    ReviewStatus,
    review_circuit,
)

from . import blocks as B  # noqa: E402
from .commands import HELP, Command, CommandError  # noqa: E402
from .config import Config  # noqa: E402
from .order import draft_blocks, draft_json, prepare_order  # noqa: E402
from .render import render_board  # noqa: E402
from .slack import SlackClient, SlackError  # noqa: E402

__all__ = ["Runner", "RunRecord", "RunStore", "ThreadKey"]

log = logging.getLogger("slackbot.runner")

#: How often the progress message may be edited. Slack rate-limits
#: ``chat.update`` per channel, and a run has more stage events than a reader
#: has attention, so most events only update state and do not post.
PROGRESS_INTERVAL_S = 3.0

ThreadKey = tuple[str, str]


@dataclass
class RunRecord:
    """The last finished run in one thread.

    Held in memory only. A restart loses it, and `review`/`order` then say so
    rather than silently acting on a different run -- which is the failure mode
    a persisted store would have to be designed to avoid anyway.
    """

    run_id: str
    command: Command
    result: Any
    artifacts: list[Path] = field(default_factory=list)
    finished_at: float = field(default_factory=time.time)


class RunStore:
    """Thread-keyed memory of finished runs, bounded so it cannot grow."""

    def __init__(self, limit: int = 200):
        self._limit = limit
        self._lock = threading.Lock()
        self._runs: dict[ThreadKey, RunRecord] = {}

    def put(self, key: ThreadKey, record: RunRecord) -> None:
        with self._lock:
            self._runs[key] = record
            while len(self._runs) > self._limit:
                oldest = min(self._runs, key=lambda k: self._runs[k].finished_at)
                del self._runs[oldest]

    def get(self, key: ThreadKey) -> RunRecord | None:
        with self._lock:
            return self._runs.get(key)


class Runner:
    """Runs commands. One instance serves the whole workspace."""

    def __init__(
        self,
        config: Config,
        client: SlackClient,
        *,
        model_factory: Callable[[], Model] | None = None,
        store: RunStore | None = None,
    ):
        self._config = config
        self._client = client
        self._model_factory = model_factory or (lambda: GeminiModel(config.model))
        self._store = store or RunStore()
        # A design run costs real model calls and a CP-SAT solve. The semaphore
        # is what stops six people in a channel from starting six at once.
        self._slots = threading.BoundedSemaphore(config.max_concurrent_runs)

    # -- entry point ------------------------------------------------------

    def handle(
        self, command: Command, *, channel: str, thread_ts: str, user: str = ""
    ) -> None:
        """Execute one command, reporting everything into its thread.

        ``thread_ts`` is empty for a slash command, which has no message to
        thread under. A design run makes its own anchor and threads beneath it
        (see :meth:`_run_design`); a follow-up verb cannot, because "the last
        run in this channel" is not a thing a second person's `order` should
        silently resolve to. So those are refused with an explanation rather
        than answered from the wrong board.
        """
        key: ThreadKey = (channel, thread_ts)
        try:
            if not thread_ts and command.needs_prior_run():
                raise CommandError(
                    f"`{command.verb}` works on the run in its own thread, so "
                    "run it as a reply in the thread of a design run rather "
                    "than as a slash command."
                )
            if command.verb == "help":
                self._post(channel, thread_ts, "silkscreen help", B.help_blocks(HELP))
            elif command.verb in ("design", "place"):
                self._run_design(command, key, user)
            elif command.verb == "review":
                self._run_review(command, key)
            elif command.verb == "order":
                self._run_order(command, key)
            else:  # pragma: no cover - parse_command cannot produce this
                raise CommandError(f"I don't know how to `{command.verb}`.")
        except CommandError as exc:
            self._post(
                channel,
                thread_ts,
                str(exc),
                B.error_blocks(
                    "I couldn't run that", str(exc), "Try `@silkscreen help`."
                ),
            )
        except SlackError:
            # Slack itself failed; there is no channel left to complain into.
            log.exception("slack call failed for %s", command.verb)
        except Exception as exc:  # noqa: BLE001 - a run must not kill the worker
            log.exception("run failed: %s", command.verb)
            self._report_failure(channel, thread_ts, exc)

    def report_error(self, channel: str, thread_ts: str, message: str) -> None:
        """Relay a message that never became a command, e.g. a parse error."""
        try:
            self._post(
                channel,
                thread_ts,
                message,
                B.error_blocks(
                    "I couldn't run that", message, "Try `@silkscreen help`."
                ),
            )
        except SlackError:
            log.exception("could not report a parse error to %s", channel)

    def report_busy(self, channel: str, thread_ts: str) -> None:
        """Say the bot is saturated, rather than dropping the request quietly."""
        try:
            self._post(
                channel,
                thread_ts,
                "silkscreen is busy",
                B.error_blocks(
                    "I'm at capacity",
                    f"{self._config.max_concurrent_runs} run(s) are already in "
                    "flight and this one waited too long for a slot.",
                    "Ask again when the runs above have finished.",
                ),
            )
        except SlackError:
            log.exception("could not report saturation to %s", channel)

    # -- verbs ------------------------------------------------------------

    def _run_design(self, command: Command, key: ThreadKey, user: str) -> None:
        channel, thread_ts = key
        run_id = uuid.uuid4().hex[:10]
        started = time.monotonic()

        progress_ts = self._post(
            channel,
            thread_ts,
            f"silkscreen: working on “{command.intent[:120]}”",
            B.accepted_blocks(command.intent, user),
        )
        anchored = not thread_ts
        if anchored:
            # A slash command had nothing to thread under, so this first
            # message becomes the anchor and the run hangs off it. Without
            # this every slash-command run in a channel shares the key
            # ``(channel, "")``, and a later `review` or `order` would act on
            # whichever run happened to finish last -- a different person's
            # board, reported as though it were yours.
            thread_ts = progress_ts
            key = (channel, thread_ts)
        tracker = _Progress(
            self._client, channel, progress_ts, command.intent, started
        )

        try:
            model = self._model_factory()
            output = self._config.workdir / run_id / "board.kicad_pcb"
            output.parent.mkdir(parents=True, exist_ok=True)

            result = generate_pcb(
                model,
                command.intent,
                datasheets=command.datasheets or None,
                output=output,
                max_repairs=self._config.max_repairs,
                time_limit_s=self._config.time_limit_s,
                review=command.review,
                on_event=tracker.on_event,
            )
        except SlackError:
            raise
        except Exception as exc:  # noqa: BLE001 - the thread must hear it
            if not anchored:
                raise
            # An anchored run made its own thread above; ``handle``'s outer
            # handler only knows the original empty thread_ts, so reporting
            # from there would put the failure top-level in the channel while
            # the anchored thread sat stuck on its progress message forever.
            log.exception("run failed: %s", command.verb)
            self._report_failure(channel, thread_ts, exc)
            return
        duration = time.monotonic() - started

        artifacts = [p for p in (result.board_path,) if p]
        self._store.put(
            key,
            RunRecord(
                run_id=run_id,
                command=command,
                result=result,
                artifacts=list(artifacts),
            ),
        )

        tracker.finish()
        self._post(
            channel,
            thread_ts,
            B.summary_text(result),
            B.result_blocks(result, duration_s=duration, reviewed=command.review),
        )
        self._upload_artifacts(channel, thread_ts, result, run_id, artifacts)

    def _run_review(self, command: Command, key: ThreadKey) -> None:
        channel, thread_ts = key
        record = self._require_record(key)
        result = record.result
        try:
            # ``review_circuit`` returns ``(findings, dropped)`` -- binding the
            # pair to one name put a list and a tuple inside
            # ``ReviewReport(findings=...)``, so the thread reported two
            # findings that were really the two halves of the return value.
            findings, _dropped = review_circuit(
                self._model_factory(), result.spec, facts=list(result.facts)
            )
        except ReviewError as exc:
            # The critic answered something unreadable. Say so and keep the
            # previous pass's findings: overwriting them with [] would turn a
            # model failure into a thread that reports a clean board, which is
            # the one thing this must never do.
            report = ReviewReport(status=ReviewStatus.FAILED, detail=str(exc))
            result.review = report
            self._post(
                channel,
                thread_ts,
                f"silkscreen review: {report.note()}",
                B.findings_blocks(list(result.findings), report=report),
            )
            return
        # Replace rather than append: this is the same design re-examined, and
        # showing both passes would double-count every finding they agree on.
        report = ReviewReport(status=ReviewStatus.OK, findings=tuple(findings))
        result.findings = list(findings)
        result.review = report
        self._post(
            channel,
            thread_ts,
            f"silkscreen review: {len(findings)} finding(s)",
            B.findings_blocks(list(findings), report=report),
        )

    def _run_order(self, command: Command, key: ThreadKey) -> None:
        channel, thread_ts = key
        record = self._require_record(key)
        draft = prepare_order(
            record.result,
            quantity=command.quantity,
            artifacts=[p.name for p in record.artifacts],
        )
        self._post(
            channel,
            thread_ts,
            f"silkscreen: order draft for {draft.quantity} boards (not submitted)",
            draft_blocks(draft),
        )
        payload = json.dumps(draft_json(draft), indent=2).encode("utf-8")
        self._upload(
            channel,
            thread_ts,
            f"order-draft-{record.run_id}.json",
            payload,
            "Order draft — review before ordering. Nothing has been submitted.",
        )

    # -- helpers ----------------------------------------------------------

    def _require_record(self, key: ThreadKey) -> RunRecord:
        record = self._store.get(key)
        if record is None:
            raise CommandError(
                "I don't have a finished run in this thread. Start one with "
                "`@silkscreen design <what you want>`, then ask again here. "
                "(Runs are remembered in memory, so a restart forgets them.)"
            )
        return record

    def _upload_artifacts(
        self,
        channel: str,
        thread_ts: str,
        result: Any,
        run_id: str,
        artifacts: list[Path],
    ) -> None:
        """Post the picture and then the board file.

        Order matters: the image is the thing a person reacts to in a channel,
        and the ``.kicad_pcb`` is the thing they actually open. Neither failing
        is allowed to hide the other, so each is attempted separately.
        """
        try:
            image = render_board(result.board, stem=f"board-{run_id}")
            self._upload(
                channel,
                thread_ts,
                image.filename,
                image.content,
                "Placement preview — the board file below is the deliverable.",
            )
        except Exception:  # noqa: BLE001 - a preview is not worth losing a run
            log.exception("board preview failed for run %s", run_id)

        for path in artifacts:
            try:
                self._upload(
                    channel,
                    thread_ts,
                    path.name,
                    path.read_bytes(),
                    "Open this in KiCad — that is the supported path.",
                )
            except Exception:  # noqa: BLE001
                log.exception("upload failed for %s", path)

    def _upload(
        self,
        channel: str,
        thread_ts: str,
        filename: str,
        content: bytes,
        comment: str,
    ) -> None:
        self._client.upload_file(
            channel,
            filename,
            content,
            thread_ts=thread_ts or None,
            initial_comment=comment,
        )

    def _post(
        self,
        channel: str,
        thread_ts: str,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
    ) -> str:
        # An empty thread_ts means "no thread", and Slack rejects the empty
        # string where it accepts an absent field.
        return self._client.post_message(
            channel, text, thread_ts=thread_ts or None, blocks=blocks
        )

    def _report_failure(self, channel: str, thread_ts: str, exc: Exception) -> None:
        """Say what went wrong in the terms the reader can act on.

        A missing API key is a setup problem, a model outage is a wait-and-retry
        problem, and a proposal that never validated is a prompt problem. They
        are three different next actions, so they get three different messages
        rather than one stack trace.
        """
        if isinstance(exc, ModelError):
            title = "The model call failed"
            hint = (
                "Check `GOOGLE_API_KEY` where the bot runs, or retry — this is "
                "usually a transient upstream error."
            )
        elif type(exc).__name__ == "ProposalError":
            title = "I couldn't produce a circuit that validated"
            hint = (
                "The proposal failed structural validation every time. A more "
                "specific intent — name the parts and the voltages — usually fixes it."
            )
        elif type(exc).__name__ == "UnsupportedPackage":
            title = "No footprint rule for one of those parts"
            hint = "Supported: 3 pins (SOT-223), 4–28 even (SOIC), 32–144 (LQFP)."
        else:
            title = "That run failed"
            hint = "The full traceback is in the bot's own logs."
        detail = str(exc)[:800]
        self._post(channel, thread_ts, f"silkscreen: {title}", B.error_blocks(
            title, detail, hint
        ))

    # -- concurrency ------------------------------------------------------

    def acquire_slot(self, timeout: float = 0.0) -> bool:
        return self._slots.acquire(blocking=timeout > 0, timeout=timeout or None)

    def release_slot(self) -> None:
        self._slots.release()


class _Progress:
    """Tracks stage events and edits one message rather than posting many.

    A stage is ticked only when its own event arrives. If the pipeline stalls,
    the list stops advancing and the clock keeps running -- the same honesty
    rule the web UI's running view follows, for the same reason: a progress
    display that predicts stages is a progress display that lies.
    """

    def __init__(
        self,
        client: SlackClient,
        channel: str,
        ts: str,
        intent: str,
        started: float,
    ):
        self._client = client
        self._channel = channel
        self._ts = ts
        self._intent = intent
        self._started = started
        self._done: list[str] = []
        self._current = ""
        self._last_post = 0.0

    def on_event(self, event: dict[str, Any]) -> None:
        name = event.get("event")
        stage = str(event.get("stage") or "")
        changed = False
        if name == "stage.start" and stage:
            self._current = stage
            changed = True
        elif name == "stage.done" and stage:
            if stage not in self._done:
                self._done.append(stage)
            # Propose finishing means the repair loop inside it finished too.
            if stage == "propose" and "validate" not in self._done:
                self._done.append("validate")
            self._current = ""
            changed = True
        elif name == "propose.round":
            self._current = "validate"
            changed = True
        if changed:
            self._maybe_update()

    def finish(self) -> None:
        self._current = ""
        self._push()

    def _maybe_update(self) -> None:
        now = time.monotonic()
        if now - self._last_post < PROGRESS_INTERVAL_S:
            return
        self._push()

    def _push(self) -> None:
        self._last_post = time.monotonic()
        elapsed = time.monotonic() - self._started
        try:
            self._client.update_message(
                self._channel,
                self._ts,
                "silkscreen: working…",
                blocks=B.progress_blocks(
                    self._intent, self._done, self._current, elapsed
                ),
            )
        except SlackError:
            # A dropped progress edit is cosmetic. Raising here would abort the
            # run, because generate_pcb treats a callback exception as a cancel.
            log.debug("progress update dropped", exc_info=True)
