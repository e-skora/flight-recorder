"""View models for the decision-detail screen, read from the projection tables.

`PRODUCT.md` §4.4 asks one screen to show everything that was recorded about a
decision: its timestamps, class and output, score and threshold, workflow and
logic versions, the immutable logic-artifact identity and its declarative
ruleset, the preserved historical context with each input's state, contribution
and provenance, the persisted explanation, the downstream action and cost, and
the later outcome observations. Every one of those values is already stored;
this module only reads them and turns them into text.

**Why these reads are separate from `replay/`'s.** `replay/reconstruct.py` and
`replay/counterfactual.py` have a deliberately narrow read profile, proven in
`tests/invariants/test_inv_06_separation.py`: they never touch `events` or
`accounts`, and they read `evidence_versions` only by the id the decision
preserved, with no ordering and no supersession link. A page needs more than
that -- an event's `source` and `recorded_at`, an evidence version's `source`,
the account's actions and outcomes, the registry of artifacts to choose from --
so it issues its own selects here rather than widening the reconstruction path.
Nothing in this module is imported by `replay/`.

**Nothing here is derived beyond formatting.** The four recorded input states
(`consumed`, `available but ignored`, `unavailable`, `absent`) come from the
record itself: availability as the collector stored it, consumption as
`decision_consumed_inputs` records it, absence as the historical artifact
referencing a key the preserved context has no row for (INV-03). No score,
threshold, contribution, classification, or outcome status is computed here.
The replay panel's numbers come straight off the engine's `Comparison`.

**Nothing is persisted.** No counterfactual is written, cached, or memoized
anywhere; every page load recomputes it from the ledger (D-011, INV-06).

**Attribution is not evaluated by this code.** Outcomes are rendered at their
recorded scope -- observations recorded for the account -- with any recorded
action or decision reference shown as a recorded reference. Whether an outcome
is linked to a decision is `outcome-attribution-v1`'s question: this module
reads the persisted result through the policy module's two selection
operations -- the effective outcome version first, then the effective result
of that exact version -- and never computes, approximates or inherits one
(D-013). An outcome with no persisted result is shown as not yet evaluated,
which is neither `unresolved` nor a failure, and an unresolved outcome is
listed like any other.
"""

import json
from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy import select

from flight_recorder.attribution.policy import (
    POLICY_VERSION,
    AttributionError,
    effective_attribution,
    effective_outcome_version,
    ledger_maximum,
)
from flight_recorder.collector.schema import LogicArtifact, format_utc
from flight_recorder.ledger.schema import (
    actions,
    decision_consumed_inputs,
    decision_context,
    decisions,
    events,
    evidence_versions,
    logic_artifacts,
    outcomes,
)
from flight_recorder.logic.evaluator import (
    DuplicateContextKey,
    EvaluationError,
    UnsupportedMissingValueBehavior,
)
from flight_recorder.logic.rules import (
    RuleError,
    RuleKeyMismatch,
    RuleTypeError,
    UnsupportedBoundary,
    UnsupportedRule,
)
from flight_recorder.replay.reconstruct import (
    ArtifactMissing,
    DecisionNotFound,
    IntegrityFailure,
    ReconstructionError,
    ReconstructionMismatch,
)

__all__ = [
    "ABSENT",
    "ARTIFACT_UNREADABLE",
    "AVAILABLE_BUT_IGNORED",
    "CONSUMED",
    "CONTEXT_WITHOUT_ARTIFACT",
    "NOT_CONSUMED",
    "ORIGIN_RECORDED_LOGIC",
    "ORIGIN_SELECTED_ARTIFACT",
    "UNAVAILABLE",
    "OBSERVATION_UNKNOWN",
    "OBSERVED_NO",
    "OBSERVED_YES",
    "ActionRow",
    "ArtifactOption",
    "AttributionFailureView",
    "AttributionView",
    "ContextRow",
    "DecisionPage",
    "DecisionView",
    "FailureField",
    "OutcomeRow",
    "ReplayFailure",
    "RulesetFactorRow",
    "RulesetView",
    "artifact_options",
    "failure_view",
    "load_decision_page",
]

#: The four recorded input states, as the words the page renders (INV-03).
#: They are the *recorded* states, derived from the record alone; the engine's
#: `InputState` describes an evaluation and belongs to the replay panel.
CONSUMED = "consumed"
AVAILABLE_BUT_IGNORED = "available but ignored"
UNAVAILABLE = "unavailable"
ABSENT = "absent"

#: The contribution cell for every state other than `consumed`. Never `0`: a
#: contribution of zero is a consumed input whose rule did not match, which is
#: a different fact from an input the logic never consumed.
NOT_CONSUMED = "not consumed"

#: What the ruleset and the `absent` rows need and cannot get when the recorded
#: logic artifact is unreadable.
ARTIFACT_UNREADABLE = (
    "The recorded logic artifact could not be read, so the ruleset is unavailable."
)
CONTEXT_WITHOUT_ARTIFACT = (
    "The recorded logic artifact could not be read, so any input its factors reference "
    "that the preserved context has no row for cannot be listed here."
)


# --- View models ------------------------------------------------------------


@dataclass(frozen=True)
class DecisionView:
    """The `decisions` row and its `events` row, as text."""

    decision_event_id: str
    account_ref: str
    decision_class: str
    decision_boundary: str
    workflow_version: str
    artifact_hash: str
    logic_version: str
    evaluator_version: str
    score: int
    threshold: int
    output: str
    explanation: str | None
    occurred_at: str
    recorded_at: str
    source: str
    #: From the `logic_artifacts` row's own columns; None when it is missing.
    artifact_id: str | None
    artifact_schema_version: str | None


@dataclass(frozen=True)
class RulesetFactorRow:
    key: str
    rule: str
    weight: int


@dataclass(frozen=True)
class RulesetView:
    """The declarative ruleset exactly as it was recorded, or an explicit gap."""

    available: bool
    unavailable_note: str | None
    factors: tuple[RulesetFactorRow, ...] = ()
    threshold: int | None = None
    at_or_above_threshold: str | None = None
    below_threshold: str | None = None
    missing_value_behavior: str | None = None
    activated_at: str | None = None
    deactivated_at: str | None = None
    activation_status: str | None = None


@dataclass(frozen=True)
class ContextRow:
    """One preserved input of `H(d)`, with its recorded state and provenance."""

    input_key: str
    value_display: str
    state: str
    contribution_display: str
    evidence_version_id: str | None
    source: str | None
    observed_at: str | None
    available_at: str | None


@dataclass(frozen=True)
class ActionRow:
    action_event_id: str
    action_type: str
    play_id: int
    target_persona: str
    status: str
    cost: str
    currency: str
    occurred_at: str


#: The three states of one recorded observation (INV-09). `unknown` is a v2
#: observation recorded as unknown; it is never rendered as `no`.
OBSERVED_YES = "yes"
OBSERVED_NO = "no"
OBSERVATION_UNKNOWN = "unknown"


@dataclass(frozen=True)
class AttributionView:
    """The effective persisted attribution of one exact outcome version."""

    attribution_event_id: str
    status: str
    policy_version: str
    method: str
    window_days: int
    resolved_action_event_id: str | None
    resolved_decision_event_id: str | None
    reason: str
    attributed_at: str
    ingest_cutoff: int
    heuristic: bool
    resolves_to_this_decision: bool


@dataclass(frozen=True)
class AttributionFailureView:
    """A named failure selecting the effective version or result.

    Distinct from `unresolved` (a result) and from no result at all.
    """

    reason: str
    message: str


@dataclass(frozen=True)
class OutcomeRow:
    """One recorded outcome version for the decision's account.

    The three observations are carried as three states -- `yes`, `no`,
    `unknown` -- and rendered as words, never bare booleans and never blanks.
    A v1 row records a window length and no window state; a v2 row records its
    period bounds, an explicit evaluation state and an as-of instant, and in an
    open window `no` means nothing recorded as of that instant.
    `references_this_decision` and `source_decision_is_this_decision` report
    what the recorded reference or claim says and make no attribution claim;
    only `attribution`, the persisted policy result, does (D-013, INV-08).
    """

    outcome_event_id: str
    schema_version: str
    window_days: int | None
    window_opened_at: str | None
    window_closes_at: str | None
    evaluation_state: str | None
    observed_at: str | None
    reply_state: str
    meeting_state: str
    opportunity_state: str
    reply_display: str
    meeting_display: str
    opportunity_display: str
    occurred_at: str
    recorded_at: str
    action_event_id: str | None
    referenced_decision_event_id: str | None
    references_this_decision: bool
    source_action_event_id: str | None
    source_decision_event_id: str | None
    source_decision_is_this_decision: bool
    supersedes_outcome_event_id: str | None
    #: The version of this row's chain that nothing supersedes; this row's own
    #: id when the row is effective. None only when selection failed.
    effective_outcome_event_id: str | None
    #: None when this exact version has no persisted result.
    attribution: AttributionView | None
    attribution_failure: AttributionFailureView | None

    @property
    def superseded(self) -> bool:
        return (
            self.effective_outcome_event_id is not None
            and self.effective_outcome_event_id != self.outcome_event_id
        )


@dataclass(frozen=True)
class ArtifactOption:
    """One registered artifact for this decision class, selectable by exact hash."""

    artifact_hash: str
    logic_version: str
    evaluator_version: str
    label: str
    selected: bool


@dataclass(frozen=True)
class FailureField:
    """One named attribute of a failure, rendered as one line per value.

    A `ReconstructionMismatch` on `consumed_inputs` carries sorted lists of
    `(input_key, evidence_version_id, contribution)` triples; each becomes its
    own line rather than one unreadable Python repr.
    """

    name: str
    lines: tuple[str, ...]


@dataclass(frozen=True)
class ReplayFailure:
    """One replay failure, named and readable, for the failure region.

    The failure is a `ReconstructionError`, a `RuleError` or an
    `EvaluationError`. `origin` is the sentence saying which side of the replay
    raised it -- the decision's own recorded logic, or the selected current
    artifact -- and is None only when the caller did not establish it.
    """

    class_name: str
    summary: str
    message: str
    fields: tuple[FailureField, ...]
    origin: str | None = None


@dataclass(frozen=True)
class DecisionPage:
    """Everything the decision-detail screen renders from the record alone."""

    decision: DecisionView
    ruleset: RulesetView
    context_rows: tuple[ContextRow, ...]
    context_note: str | None
    action_rows: tuple[ActionRow, ...]
    outcome_rows: tuple[OutcomeRow, ...]


# --- Formatting helpers -----------------------------------------------------


def _value_display(value_text: str | None) -> str:
    """The preserved scalar as text.

    `decision_context.value_text` is canonical JSON, so `184`, `"184"` and
    `true` are stored differently and must be decoded rather than printed raw.
    """
    if value_text is None:
        return "no value recorded"
    try:
        decoded = json.loads(value_text)
    except ValueError:
        return value_text
    if decoded is None:
        return "no value recorded"
    if isinstance(decoded, bool):
        return "true" if decoded else "false"
    return str(decoded)


def _observation_state(flag: bool | None) -> str:
    if flag is None:
        return OBSERVATION_UNKNOWN
    return OBSERVED_YES if flag else OBSERVED_NO


def artifact_label(logic_version: str, artifact_hash: str, evaluator_version: str) -> str:
    """`<logic_version> · <first 12 hex of hash> · <evaluator_version>`."""
    return f"{logic_version} · {artifact_hash[:12]} · {evaluator_version}"


# --- Reads ------------------------------------------------------------------


def _artifact_row(conn, artifact_hash: str):
    return conn.execute(
        select(logic_artifacts).where(logic_artifacts.c.artifact_hash == artifact_hash)
    ).first()


def _decoded_artifact(row) -> LogicArtifact | None:
    """The recorded artifact through the strict model, or None if unreadable.

    Unreadable is a page state, not a crash: every recorded field that does not
    depend on the artifact still renders (INV-09).
    """
    if row is None:
        return None
    try:
        return LogicArtifact.model_validate_json(row.artifact_json, strict=True)
    except (ValidationError, ValueError):
        return None


def _ruleset_view(artifact: LogicArtifact | None) -> RulesetView:
    if artifact is None:
        return RulesetView(available=False, unavailable_note=ARTIFACT_UNREADABLE)
    activation = artifact.activation
    return RulesetView(
        available=True,
        unavailable_note=None,
        factors=tuple(
            RulesetFactorRow(key=f.key, rule=f.rule, weight=f.weight) for f in artifact.factors
        ),
        threshold=artifact.threshold,
        at_or_above_threshold=artifact.output_mapping.at_or_above_threshold,
        below_threshold=artifact.output_mapping.below_threshold,
        missing_value_behavior=artifact.missing_value_behavior,
        activated_at=format_utc(activation.activated_at),
        deactivated_at=(
            format_utc(activation.deactivated_at) if activation.deactivated_at is not None else None
        ),
        activation_status=activation.status,
    )


def _context_rows(
    conn, decision_event_id: str, artifact: LogicArtifact | None
) -> tuple[tuple[ContextRow, ...], str | None]:
    """`H(d)` as rows, with each input's recorded state (INV-03).

    `decision_context` is left joined to `evidence_versions` so an explicitly
    unavailable input, which references no evidence version, still produces a
    row rather than disappearing from the table.
    """
    preserved = conn.execute(
        select(
            decision_context.c.input_key,
            decision_context.c.availability,
            decision_context.c.value_text,
            decision_context.c.evidence_version_id,
            evidence_versions.c.source,
            evidence_versions.c.observed_at,
            evidence_versions.c.available_at,
        )
        .select_from(
            decision_context.outerjoin(
                evidence_versions,
                decision_context.c.evidence_version_id == evidence_versions.c.evidence_version_id,
            )
        )
        .where(decision_context.c.decision_event_id == decision_event_id)
        .order_by(decision_context.c.input_key)
    ).all()

    consumed = {
        row.input_key: row.contribution
        for row in conn.execute(
            select(
                decision_consumed_inputs.c.input_key,
                decision_consumed_inputs.c.contribution,
            ).where(decision_consumed_inputs.c.decision_event_id == decision_event_id)
        )
    }

    rows: list[ContextRow] = []
    for row in preserved:
        if row.availability == UNAVAILABLE:
            state, contribution = UNAVAILABLE, NOT_CONSUMED
        elif row.input_key in consumed:
            state, contribution = CONSUMED, str(consumed[row.input_key])
        else:
            state, contribution = AVAILABLE_BUT_IGNORED, NOT_CONSUMED
        rows.append(
            ContextRow(
                input_key=row.input_key,
                value_display=_value_display(row.value_text),
                state=state,
                contribution_display=contribution,
                evidence_version_id=row.evidence_version_id,
                source=row.source,
                observed_at=row.observed_at,
                available_at=row.available_at,
            )
        )

    note = None
    if artifact is None:
        note = CONTEXT_WITHOUT_ARTIFACT
    else:
        recorded_keys = {row.input_key for row in preserved}
        for key in sorted({f.key for f in artifact.factors} - recorded_keys):
            rows.append(
                ContextRow(
                    input_key=key,
                    value_display="no value recorded",
                    state=ABSENT,
                    contribution_display=NOT_CONSUMED,
                    evidence_version_id=None,
                    source=None,
                    observed_at=None,
                    available_at=None,
                )
            )

    rows.sort(key=lambda row: row.input_key)
    return tuple(rows), note


def _action_rows(conn, decision_event_id: str) -> tuple[ActionRow, ...]:
    rows = conn.execute(
        select(actions)
        .where(actions.c.decision_event_id == decision_event_id)
        .order_by(actions.c.occurred_at, actions.c.action_event_id)
    ).all()
    return tuple(
        ActionRow(
            action_event_id=row.action_event_id,
            action_type=row.action_type,
            play_id=row.play_id,
            target_persona=row.target_persona,
            status=row.status,
            cost=row.cost,
            currency=row.currency,
            occurred_at=row.occurred_at,
        )
        for row in rows
    )


def _attribution_view(stored, decision_event_id: str) -> AttributionView | None:
    if stored is None:
        return None
    return AttributionView(
        attribution_event_id=stored.attribution_event_id,
        status=stored.status,
        policy_version=stored.policy_version,
        method=stored.method,
        window_days=stored.window_days,
        resolved_action_event_id=stored.resolved_action_event_id,
        resolved_decision_event_id=stored.resolved_decision_event_id,
        reason=stored.reason,
        attributed_at=stored.attributed_at,
        ingest_cutoff=stored.ingest_cutoff,
        heuristic=stored.heuristic,
        resolves_to_this_decision=stored.resolved_decision_event_id == decision_event_id,
    )


def _outcome_rows(conn, account_ref: str, decision_event_id: str) -> tuple[OutcomeRow, ...]:
    """Every recorded outcome version for the *account*, which is their recorded scope.

    Left joined to `actions` so an outcome carrying no action reference still
    produces a row; the join recovers only what the record says. Superseded
    versions stay listed and inspectable. Each row's attribution is selected
    for that exact version through the policy module's selection operations at
    the ledger's current maximum sequence, so unresolved and unattributed
    outcomes are listed exactly like resolved ones, and a corrected version
    never shows its predecessor's result.
    """
    rows = conn.execute(
        select(
            outcomes,
            actions.c.decision_event_id.label("referenced_decision_event_id"),
        )
        .select_from(
            outcomes.outerjoin(actions, outcomes.c.action_event_id == actions.c.action_event_id)
        )
        .where(outcomes.c.account_ref == account_ref)
        .order_by(outcomes.c.occurred_at, outcomes.c.outcome_event_id)
    ).all()
    cutoff = ledger_maximum(conn)

    result = []
    for row in rows:
        effective = attribution = failure = None
        try:
            effective = effective_outcome_version(conn, row.outcome_event_id, cutoff=cutoff)
            attribution = _attribution_view(
                effective_attribution(conn, row.outcome_event_id, POLICY_VERSION, cutoff=cutoff),
                decision_event_id,
            )
        except AttributionError as error:
            failure = AttributionFailureView(reason=error.reason, message=str(error))
        states = {
            word: _observation_state(getattr(row, word))
            for word in ("reply", "meeting", "opportunity")
        }
        result.append(
            OutcomeRow(
                outcome_event_id=row.outcome_event_id,
                schema_version=row.schema_version,
                window_days=row.window_days,
                window_opened_at=row.window_opened_at,
                window_closes_at=row.window_closes_at,
                evaluation_state=row.evaluation_state,
                observed_at=row.observed_at,
                reply_state=states["reply"],
                meeting_state=states["meeting"],
                opportunity_state=states["opportunity"],
                reply_display=f"reply: {states['reply']}",
                meeting_display=f"meeting: {states['meeting']}",
                opportunity_display=f"opportunity: {states['opportunity']}",
                occurred_at=row.occurred_at,
                recorded_at=row.recorded_at,
                action_event_id=row.action_event_id,
                referenced_decision_event_id=row.referenced_decision_event_id,
                references_this_decision=row.referenced_decision_event_id == decision_event_id,
                source_action_event_id=row.source_action_event_id,
                source_decision_event_id=row.source_decision_event_id,
                source_decision_is_this_decision=row.source_decision_event_id == decision_event_id,
                supersedes_outcome_event_id=row.supersedes_outcome_event_id,
                effective_outcome_event_id=effective,
                attribution=attribution,
                attribution_failure=failure,
            )
        )
    return tuple(result)


def artifact_options(
    conn, decision_class: str, selected_hash: str | None
) -> tuple[ArtifactOption, ...]:
    """Every registered artifact for this decision class, in display order.

    Ordered by (`logic_version`, `artifact_hash`): a deterministic display
    order, explicitly *not* a recency order. The current artifact is always an
    explicitly selected hash, never "the latest" (D-011).
    """
    rows = conn.execute(
        select(
            logic_artifacts.c.artifact_hash,
            logic_artifacts.c.logic_version,
            logic_artifacts.c.evaluator_version,
        )
        .where(logic_artifacts.c.decision_class == decision_class)
        .order_by(logic_artifacts.c.logic_version, logic_artifacts.c.artifact_hash)
    ).all()
    return tuple(
        ArtifactOption(
            artifact_hash=row.artifact_hash,
            logic_version=row.logic_version,
            evaluator_version=row.evaluator_version,
            label=artifact_label(row.logic_version, row.artifact_hash, row.evaluator_version),
            selected=row.artifact_hash == selected_hash,
        )
        for row in rows
    )


def load_decision_page(conn, decision_event_id: str) -> DecisionPage | None:
    """Every recorded field the decision-detail screen shows, or None.

    None means no `decisions` row with that id; the caller turns that into a
    404. Construction never raises because the recorded logic artifact is
    missing or malformed: the artifact-dependent sections degrade explicitly
    instead (INV-09).
    """
    decision = conn.execute(
        select(decisions).where(decisions.c.decision_event_id == decision_event_id)
    ).first()
    if decision is None:
        return None

    event = conn.execute(
        select(events.c.occurred_at, events.c.recorded_at, events.c.source).where(
            events.c.event_id == decision_event_id
        )
    ).first()

    artifact_row = _artifact_row(conn, decision.artifact_hash)
    artifact = _decoded_artifact(artifact_row)
    context_rows, context_note = _context_rows(conn, decision_event_id, artifact)

    view = DecisionView(
        decision_event_id=decision.decision_event_id,
        account_ref=decision.account_ref,
        decision_class=decision.decision_class,
        decision_boundary=decision.decision_boundary,
        workflow_version=decision.workflow_version,
        artifact_hash=decision.artifact_hash,
        logic_version=decision.logic_version,
        evaluator_version=decision.evaluator_version,
        score=decision.score,
        threshold=decision.threshold,
        output=decision.output,
        explanation=decision.explanation,
        occurred_at=event.occurred_at if event is not None else "",
        recorded_at=event.recorded_at if event is not None else "",
        source=event.source if event is not None else "",
        artifact_id=artifact_row.artifact_id if artifact_row is not None else None,
        artifact_schema_version=(
            artifact_row.artifact_schema_version if artifact_row is not None else None
        ),
    )
    return DecisionPage(
        decision=view,
        ruleset=_ruleset_view(artifact),
        context_rows=context_rows,
        context_note=context_note,
        action_rows=_action_rows(conn, decision_event_id),
        outcome_rows=_outcome_rows(conn, decision.account_ref, decision_event_id),
    )


# --- The failure view (INV-09, AC-07) ---------------------------------------


def _triple_lines(value) -> tuple[str, ...]:
    """A scalar as one line; a list of triples as one readable line each."""
    if isinstance(value, (list, tuple)):
        lines = []
        for item in value:
            if isinstance(item, (list, tuple)) and len(item) == 3:
                key, evidence_version_id, contribution = item
                lines.append(
                    f"{key} · evidence version {evidence_version_id} · contribution {contribution}"
                )
            else:
                lines.append(str(item))
        return tuple(lines) if lines else ("(none)",)
    return (str(value),)


#: What each failure class carries, under its own attribute names. A
#: `ReconstructionMismatch` is never relabelled as stored/recomputed: those mean
#: the ledger versus a recomputation, while recorded/reconstructed mean the
#: record versus the reproduction. The rule and evaluation families are keyed
#: here too: a logic artifact the collector accepts can still carry a rule the
#: closed grammar refuses or a missing-value behavior the evaluator does not
#: implement, and each must name itself on the page rather than crash it.
_FAILURE_FIELDS: dict[type, tuple[str, ...]] = {
    DecisionNotFound: ("decision_event_id",),
    ArtifactMissing: ("artifact_hash",),
    IntegrityFailure: ("field", "detail", "stored", "recomputed"),
    ReconstructionMismatch: ("field", "recorded", "reconstructed"),
    UnsupportedRule: ("key", "text"),
    RuleKeyMismatch: ("key", "rule_key", "text"),
    RuleTypeError: ("key", "detail"),
    UnsupportedBoundary: ("text", "detail"),
    UnsupportedMissingValueBehavior: ("behavior",),
    DuplicateContextKey: ("key",),
}

_FAILURE_SUMMARIES: dict[type, str] = {
    DecisionNotFound: (
        "No recorded decision with that identifier could be loaded, so nothing could be replayed."
    ),
    ArtifactMissing: (
        "The selected logic artifact is not registered, so its identity could not be "
        "established and no replay was run."
    ),
    IntegrityFailure: (
        "A verification step disagreed with the ledger, so exact replay could not be "
        "established and no counterfactual was computed."
    ),
    ReconstructionMismatch: (
        "Re-running the preserved logic over the preserved context did not reproduce the "
        "recorded decision, so no counterfactual was computed."
    ),
    UnsupportedRule: (
        "A factor's rule is not a shape this evaluator can interpret, so the logic could not "
        "be evaluated and no counterfactual was computed."
    ),
    RuleKeyMismatch: (
        "A factor's rule names a different input from the factor's own key, so the logic "
        "could not be evaluated and no counterfactual was computed."
    ),
    RuleTypeError: (
        "A factor's rule does not fit the preserved value it was applied to, so the logic "
        "could not be evaluated and no counterfactual was computed."
    ),
    UnsupportedBoundary: (
        "The decision boundary is not an explicit UTC instant, so nothing could be evaluated "
        "against it and no counterfactual was computed."
    ),
    UnsupportedMissingValueBehavior: (
        "The artifact declares a missing-value behavior this evaluator does not implement, so "
        "the logic could not be evaluated and no counterfactual was computed."
    ),
    DuplicateContextKey: (
        "The preserved historical context presents the same input key more than once, so it "
        "could not be evaluated and no counterfactual was computed."
    ),
}

#: Which side of the replay raised the failure. Reproducing the decision's own
#: preserved logic and evaluating the selected current artifact are different
#: facts, and the page never guesses between them from the exception's type.
ORIGIN_RECORDED_LOGIC = (
    "This failure arose while reproducing the decision's own recorded logic, not while "
    "evaluating the selected current logic artifact."
)
ORIGIN_SELECTED_ARTIFACT = (
    "This failure arose while evaluating the selected current logic artifact, not while "
    "reproducing the decision's own recorded logic."
)

_GENERIC_SUMMARY = (
    "Replay could not be established for this decision, so no counterfactual was computed."
)


def failure_view(
    error: ReconstructionError | RuleError | EvaluationError,
    origin: str | None = None,
) -> ReplayFailure:
    """One replay failure as a named, readable view model.

    Every class exposes the attributes it actually carries, under those names,
    and `class_name` is always the concrete class -- `UnsupportedRule`, never
    the family's base `RuleError`. The diagnostic values are shown
    deliberately: a mismatch's recorded and reconstructed values are the whole
    point of the failure, and hiding them would defeat it. Nothing here is a
    comparison or a counterfactual result.

    A family member with no entry in `_FAILURE_FIELDS` still renders. Rather
    than an empty field table it falls back to its own class name and the
    exception's message, so a failure this module has not been taught about is
    still inspectable instead of anonymous.
    """
    names = _FAILURE_FIELDS.get(type(error), ())
    fields = tuple(
        FailureField(name=name, lines=_triple_lines(getattr(error, name)))
        for name in names
        if hasattr(error, name)
    )
    if not fields:
        fields = (
            FailureField(name="failure class", lines=(type(error).__name__,)),
            FailureField(name="message", lines=(str(error),)),
        )
    return ReplayFailure(
        class_name=type(error).__name__,
        summary=_FAILURE_SUMMARIES.get(type(error), _GENERIC_SUMMARY),
        message=str(error),
        fields=fields,
        origin=origin,
    )
