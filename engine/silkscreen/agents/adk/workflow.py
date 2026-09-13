"""The pipeline as an ADK dynamic workflow.

One orchestrator node runs read, plan, propose, review, place, placement repair,
schematic and route, each as its own child node. The nodes hold no logic
of their own: every one of them calls the matching body in
:mod:`silkscreen.agents.stages`, which is what keeps the two drivers emitting
the same events. ``placement_repair``, ``schematic``, ``route``,
``review_start``, ``enclosure_start``, ``sourcing_start``, ``simulate_start``,
``review``, ``enclosure``, ``sourcing`` and ``simulate`` are always run -- each
stage body owns the decision to do nothing when its feature is off
(``review_start``, ``sourcing_start`` and ``simulate_start`` hold that switch
themselves, as the straight-line driver does, since those bodies have none),
so the graph never has to branch.

Four stages are two nodes each because they run on worker threads. The critic
is the first pair: ``review_start`` launches it as soon as the spec validates
-- it reads the spec and the datasheet facts and nothing else -- and
``review`` joins it after placement repair, so it answers inside ``place``'s
fixed CP-SAT budget instead of on the tail of the run, and exactly one
worker-model call is ever in flight. ``enclosure_start`` launches the case
right after that join, from a snapshot of the placed board, and ``enclosure``
joins it after route. Sourcing is the same pair, started right after the
enclosure and joined right after it, and the SPICE verdict is a third pair,
started and joined after sourcing.

Each node is handed the run token as ``node_input`` and returns it, so the token
is the only value ADK ever sees; the rest is looked up from the registry in
:mod:`.runner`.
"""

from __future__ import annotations

from google.adk import Context, Workflow
from google.adk.workflow import node

from ..stages import (
    EnclosureJob,
    ReviewJob,
    SimulationJob,
    SourcingJob,
    place_stage,
    placement_repair_stage,
    plan_stage,
    propose_stage,
    read_stage,
    route_stage,
    schematic_stage,
    start_enclosure_stage,
    start_review_stage,
    start_simulation_stage,
    start_sourcing_stage,
)
from .runner import recording, run_context

__all__ = ["build_workflow"]


@node(name="read")
def read(node_input: str) -> str:
    run = run_context(node_input)
    with recording(run):
        run.facts = read_stage(
            run.models.for_stage("read"),
            sheets=run.datasheets,
            preloaded_facts=run.preloaded_facts,
            emit=run.emit,
            enter=run.enter,
            unread=run.unread_datasheets,
        )
    return node_input


@node(name="plan")
def plan(node_input: str) -> str:
    """Expand the intent into a brief before anything designs against it.

    Off unless asked (`plan=False` returns None and emits nothing), and it
    never fails the run: an unusable answer leaves `plan_result.plan` None
    and propose designs from the bare intent, exactly as before.
    """
    run = run_context(node_input)
    with recording(run):
        run.plan_result = plan_stage(
            run.models.for_stage("plan"),
            intent=run.intent,
            plan=run.plan,
            max_repairs=run.max_repairs,
            emit=run.emit,
            enter=run.enter,
        )
    return node_input


@node(name="propose")
def propose(node_input: str) -> str:
    run = run_context(node_input)
    with recording(run):
        run.spec, run.attempts = propose_stage(
            run.models.for_stage("propose"),
            intent=run.intent,
            facts=run.facts,
            # Parity with the straight-line driver: the plan is what the bare
            # intent meant, so propose designs against it in both engines or
            # the two produce different boards from one request.
            brief=(
                run.plan_result.plan.brief_text()
                if run.plan_result is not None and run.plan_result.plan is not None
                else None
            ),
            max_repairs=run.max_repairs,
            emit=run.emit,
            enter=run.enter,
            propose_on_event=run.propose_on_event,
        )
    return node_input


@node(name="place")
def place(node_input: str) -> str:
    run = run_context(node_input)
    with recording(run):
        run.board = place_stage(
            run.spec,
            time_limit_s=run.time_limit_s,
            emit=run.emit,
            enter=run.enter,
        )
    return node_input


@node(name="placement_repair")
def placement_repair(node_input: str) -> str:
    run = run_context(node_input)
    with recording(run):
        run.placement = placement_repair_stage(
            run.board,
            profile=run.placement_profile,
            policy=run.placement_policy,
            feedback=run.placement_feedback,
            model=run.placement_model,
            fallback_model=run.placement_fallback_model,
            max_turns=run.placement_max_turns,
            emit=run.emit,
            enter=run.enter,
        )
        if run.placement is not None:
            run.board = run.placement.board
    return node_input


@node(name="enclosure_start")
def enclosure_start(node_input: str) -> str:
    run = run_context(node_input)
    with recording(run):
        run.enclosure_job = start_enclosure_stage(
            run.models.for_stage("enclosure"),
            run.board,
            enclosure=run.enclosure,
            enclosure_style=run.enclosure_style,
            rigorous=run.enclosure_rigorous,
            output=run.output,
            emit_stages=run.emit_stages,
            emit=run.emit,
            enter=run.enter,
        )
    return node_input


@node(name="sourcing_start")
def sourcing_start(node_input: str) -> str:
    """Start sourcing the parts on a worker thread; off means no thread."""
    run = run_context(node_input)
    with recording(run):
        run.sourcing_job = (
            start_sourcing_stage(
                run.models.for_stage("sourcing"),
                run.board,
                emit=run.emit,
                enter=run.enter,
                probe=run.sourcing_probe,
            )
            if run.sourcing
            else SourcingJob()
        )
    return node_input


@node(name="simulate_start")
def simulate_start(node_input: str) -> str:
    """Start the SPICE verdict on a worker thread; off means no thread."""
    run = run_context(node_input)
    with recording(run):
        run.simulation_job = (
            start_simulation_stage(
                run.models.for_stage("simulation"),
                run.spec,
                intent=run.intent,
                simulator=run.simulator,
                emit=run.emit,
                enter=run.enter,
            )
            if run.simulate
            else SimulationJob()
        )
    return node_input


@node(name="schematic")
def schematic(node_input: str) -> str:
    run = run_context(node_input)
    with recording(run):
        run.artifacts = schematic_stage(
            run.spec,
            run.board,
            output=run.output,
            emit_stages=run.emit_stages,
            emit=run.emit,
            enter=run.enter,
            title=run.intent,
        )
    return node_input


@node(name="route")
def route(node_input: str) -> str:
    run = run_context(node_input)
    with recording(run):
        run.route_result = route_stage(
            run.board,
            route=run.route,
            emit=run.emit,
            enter=run.enter,
        )
    return node_input


@node(name="enclosure")
def enclosure(node_input: str) -> str:
    """Join the case designed since placement; its exception surfaces here."""
    run = run_context(node_input)
    with recording(run):
        job = run.enclosure_job or EnclosureJob()
        run.enclosure_result = job.result()
    return node_input


@node(name="sourcing")
def sourcing(node_input: str) -> str:
    """Join the BOM sourced since placement; its exception surfaces here."""
    run = run_context(node_input)
    with recording(run):
        job = run.sourcing_job or SourcingJob()
        run.sourcing_result = job.result()
    return node_input


@node(name="simulate")
def simulate(node_input: str) -> str:
    """Join the verdict simulated since placement; its exception surfaces here."""
    run = run_context(node_input)
    with recording(run):
        job = run.simulation_job or SimulationJob()
        run.simulation_result = job.result()
    return node_input


@node(name="review_start")
def review_start(node_input: str) -> str:
    """Start the critic on a worker thread; off means no thread.

    The critic reads the validated spec and the datasheet facts and nothing
    else, so it starts here, at the spec, and answers inside ``place``'s
    fixed CP-SAT budget rather than on the tail of the run. The straight-line
    driver starts it at exactly the same point.
    """
    run = run_context(node_input)
    with recording(run):
        run.review_job = start_review_stage(
            run.models.for_stage("review"),
            run.spec,
            facts=run.facts,
            review=run.review,
            refute=run.refute,
            emit=run.emit,
            enter=run.enter,
        )
    return node_input


@node(name="review")
def review(node_input: str) -> str:
    """Join the critic that has been arguing since the spec was accepted."""
    run = run_context(node_input)
    with recording(run):
        job = run.review_job or ReviewJob()
        run.review_report = job.result()
    return node_input


# rerun_on_resume is ADK's condition for scheduling child nodes dynamically:
# an interrupted child wakes its parent up again to collect the result.
@node(name="silkscreen", rerun_on_resume=True)
async def silkscreen(ctx: Context, token: str) -> str:
    """The whole pipeline, in order. ``token`` binds from session state."""
    for stage in (
        # `plan` first: it is what the bare intent MEANT, and propose designs
        # against it. The SDK driver runs them in the same order.
        # `review_start` before `place`: the critic needs only the spec, and
        # place's CP-SAT budget is model-call-free time it can answer in.
        # `review` joins before the three background lanes start, so exactly
        # one worker-model call is ever in flight -- the SDK driver's rule.
        read, plan, propose, review_start, place, placement_repair, review,
        enclosure_start, sourcing_start, simulate_start,
    ):
        try:
            await ctx.run_node(stage, node_input=token)
        except BaseException:
            # A run abandoned in placement must not leave the critic running
            # on a thread nobody will ever join.
            run = run_context(token)
            if run.review_job is not None:
                run.review_job.wait()
            raise
    try:
        for stage in (schematic, route):
            await ctx.run_node(stage, node_input=token)
    except BaseException:
        # A run abandoned mid-route must not leave a paid model call running
        # on a thread nobody will ever join (the straight-line driver's rule).
        # Only on the failure path: on success the ``enclosure`` and
        # ``sourcing`` nodes join, and a blocking wait here would stall the
        # loop for nothing.
        run = run_context(token)
        for job in (run.enclosure_job, run.sourcing_job, run.simulation_job):
            if job is not None:
                job.wait()
        raise
    # Enclosure, then sourcing, then simulation: the straight-line driver's
    # join order.
    #
    # The same guard as above, and for the same reason: if the `enclosure`
    # node raises at its join, the `sourcing` and `simulate` nodes never run,
    # so their threads are never joined and whatever they raised is discarded
    # -- "a paid model call running on a thread nobody will ever join", which
    # is exactly what the block above exists to prevent. The straight-line
    # driver waits on all three in a `finally` before collecting any of them;
    # without this the two drivers disagree on the failure path.
    try:
        for stage in (enclosure, sourcing, simulate):
            await ctx.run_node(stage, node_input=token)
    except BaseException:
        run = run_context(token)
        for job in (run.enclosure_job, run.sourcing_job, run.simulation_job):
            if job is not None:
                job.wait()
        raise
    return token


def build_workflow() -> Workflow:
    """The workflow object, built fresh per run so no state is shared."""
    return Workflow(name="silkscreen", edges=[("START", silkscreen)])
