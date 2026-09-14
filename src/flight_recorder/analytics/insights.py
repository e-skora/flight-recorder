"""Insights: the read-only decision metrics of D-014 Q1 to Q3 over effective selections.

**One cutoff.** Every read is bounded by one `events.ingest_sequence` cutoff,
validated with `validate_cutoff`: decisions through `decisions.ingest_sequence`,
accounts through `accounts_query()`, outcome versions and attribution results
through the two selection operations every reader uses. Nothing is written,
cached or kept at module level.

**Order of computation (fixed).**

a. The population: `account_prioritization` decisions recorded by the cutoff
   for accounts in `accounts_query()`.
b. Effective observations: `effective_outcome_versions`, then
   `effective_attribution` for each. An `AmbiguousSelection` here propagates as
   `SelectionFailure` and no partial `Insights` exists.
c. Reconstruction of each decision through `reconstruct`. A failure of one of
   the three families the decision page names (`REPLAY_FAILURES`) is captured
   per decision as a `ReconstructionFailure`; any other exception propagates.
d. Attribution of observations to decisions: an observation is attributed to
   `d` only when its effective result is `direct` or `inferred` and resolves to
   `d`. Awaiting and unresolved observations belong to no decision, so they
   never enter a decision-level count (the AC-16 exclusion, INV-08, INV-10).
e. Standings, signals, workflows and rates, from those facts alone.

**Definitions** (D-014 Q1 controls where this text differs):

- *Observation state*: `open` (v2 open); `closed known` (v1, or v2 closed with
  a known `opportunity`); `closed unknown` (v2 closed, `opportunity` null).
- *Recorded period*: v1 `window_days`; v2 `window_closes_at - window_opened_at`
  on the stored UTC instants. *Exactly 90 days* is `window_days == 90` or a
  difference of exactly `timedelta(days=90)`.
- *Qualifying observation*: attributed, `closed known`, exactly 90 days.
- *Decision standing*, by precedence over its attributed observations of every
  period: `evaluated` (any closed known), `unknown` (any closed unknown), `open`
  (any attributed observation), else `unattributed`.
- *Rate over a set*: `eligible` = reconstructed decisions holding a qualifying
  observation; `positives` = eligible decisions holding a qualifying
  observation whose `opportunity` is true; each decision counts at most once.
  The three exclusion counts are reasons and may overlap.
- *Signals*: per decision exactly one of `known true`, `known false`,
  `unavailable`, `absent`, `not applicable`, `reconstruction failed`. A rule
  signal reads the decision's own verified factor with that key and exact rule
  text (`FactorResult.matched`); a context-value signal compares the preserved
  value in `H(d)` exactly, whether the input was consumed or ignored.

Every descriptive input (the signal definitions, the comparison workflow
version) arrives as an argument; this module opens no manifest or config. The
compared workflow version, `v4.2`, is part of D-014's metric contract itself.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from fractions import Fraction

from sqlalchemy import select

from flight_recorder.attribution.policy import (
    ATTRIBUTABLE_DECISION_CLASS,
    POLICY_VERSION,
    STATUS_DIRECT,
    STATUS_INFERRED,
    STATUS_UNRESOLVED,
    AmbiguousSelection,
    effective_attribution,
    effective_outcome_version,
    effective_outcome_versions,
    validate_cutoff,
)
from flight_recorder.collector.schema import ScalarValue, same_scalar
from flight_recorder.ledger.schema import (
    accounts_query,
    decision_consumed_inputs,
    decision_context,
    decisions,
    events,
    outcomes,
)
from flight_recorder.logic.evaluator import EvaluationError, InputState
from flight_recorder.logic.rules import RuleError
from flight_recorder.replay.reconstruct import ReconstructionError, reconstruct

__all__ = [
    "Comparison",
    "DecisionFacts",
    "DecisionStandings",
    "Insights",
    "ObservationCoverage",
    "ObservationFact",
    "Rate",
    "ReconstructionFailure",
    "RuleMatch",
    "SelectionFailure",
    "SignalDefinition",
    "SignalRow",
    "WorkflowComparison",
    "WorkflowRow",
    "decision_facts",
    "insights",
]

#: The failures the decision page names (`web/routes.py`). Never `Exception`.
REPLAY_FAILURES = (ReconstructionError, RuleError, EvaluationError)

#: The workflow version D-014's workflow comparison names.
WORKFLOW_UNDER_COMPARISON = "v4.2"

QUALIFYING_PERIOD_DAYS = 90
QUALIFYING_PERIOD = timedelta(days=QUALIFYING_PERIOD_DAYS)

AWAITING_ATTRIBUTION = "awaiting attribution"
ATTRIBUTED_STATUSES = frozenset({STATUS_DIRECT, STATUS_INFERRED})

STATE_OPEN = "open"
STATE_CLOSED_KNOWN = "closed known"
STATE_CLOSED_UNKNOWN = "closed unknown"

STANDING_EVALUATED = "evaluated"
STANDING_UNKNOWN = "unknown"
STANDING_OPEN = "open"
STANDING_UNATTRIBUTED = "unattributed"

KNOWN_TRUE = "known true"
KNOWN_FALSE = "known false"
UNAVAILABLE = "unavailable"
ABSENT = "absent"
NOT_APPLICABLE = "not applicable"
RECONSTRUCTION_FAILED = "reconstruction failed"
SIGNAL_STATES = (
    KNOWN_TRUE,
    KNOWN_FALSE,
    UNAVAILABLE,
    ABSENT,
    NOT_APPLICABLE,
    RECONSTRUCTION_FAILED,
)

KIND_RULE = "rule"
KIND_CONTEXT_VALUE = "context_value"

NOT_AVAILABLE_NOTE = "not available (0 eligible decisions)"


# --- Inputs and failures ----------------------------------------------------------


@dataclass(frozen=True)
class SignalDefinition:
    """A signal: an input key plus a rule predicate or a context-value predicate."""

    id: str
    kind: str
    input_key: str
    rule: str | None = None
    equals: ScalarValue = None

    def __post_init__(self):
        if self.kind == KIND_RULE:
            if not isinstance(self.rule, str) or not self.rule:
                raise ValueError(f"signal {self.id!r}: a rule signal needs its exact rule text")
        elif self.kind == KIND_CONTEXT_VALUE:
            if self.rule is not None:
                raise ValueError(f"signal {self.id!r}: a context-value signal has no rule text")
        else:
            raise ValueError(f"signal {self.id!r}: kind {self.kind!r} is not rule or context_value")

    @property
    def predicate(self) -> str:
        if self.kind == KIND_RULE:
            return self.rule
        return f"{self.input_key} equals {json.dumps(self.equals)}"


class SelectionFailure(Exception):
    """An effective selection is ambiguous; no partial result is returned."""

    def __init__(self, outcome_event_id: str, candidates: Sequence[str]):
        super().__init__(
            f"effective selection for outcome {outcome_event_id!r} is ambiguous: "
            + ", ".join(candidates)
        )
        self.outcome_event_id = outcome_event_id
        self.candidates = tuple(candidates)


# --- Results ------------------------------------------------------------------------


def _fraction(value: Fraction | None):
    if value is None:
        return None
    return {"denominator": value.denominator, "numerator": value.numerator}


@dataclass(frozen=True)
class Rate:
    cohort_total: int
    eligible: int
    positives: int
    excluded_other_period_only: int
    excluded_reconstruction_failed: int
    excluded_not_evaluated: int

    @property
    def available(self) -> bool:
        return self.eligible > 0

    @property
    def value(self) -> Fraction | None:
        return Fraction(self.positives, self.eligible) if self.available else None

    @property
    def display_note(self) -> str | None:
        return None if self.available else NOT_AVAILABLE_NOTE

    def as_dict(self) -> dict:
        return {
            "available": self.available,
            "cohort_total": self.cohort_total,
            "display_note": self.display_note,
            "eligible": self.eligible,
            "excluded_not_evaluated": self.excluded_not_evaluated,
            "excluded_other_period_only": self.excluded_other_period_only,
            "excluded_reconstruction_failed": self.excluded_reconstruction_failed,
            "positives": self.positives,
            "value": _fraction(self.value),
        }


@dataclass(frozen=True)
class Comparison:
    present: Rate
    absent: Rate

    @property
    def difference_points(self) -> Fraction | None:
        if not (self.present.available and self.absent.available):
            return None
        return (self.present.value - self.absent.value) * 100

    def as_dict(self) -> dict:
        return {
            "absent": self.absent.as_dict(),
            "difference_points": _fraction(self.difference_points),
            "present": self.present.as_dict(),
        }


@dataclass(frozen=True)
class ObservationCoverage:
    awaiting_attribution: int
    unresolved: int
    direct: int
    inferred: int
    open: int
    closed_known: int
    closed_unknown: int
    other_period: int
    qualifying_90_day: int
    total: int

    def as_dict(self) -> dict:
        return {
            "awaiting_attribution": self.awaiting_attribution,
            "closed_known": self.closed_known,
            "closed_unknown": self.closed_unknown,
            "direct": self.direct,
            "inferred": self.inferred,
            "open": self.open,
            "other_period": self.other_period,
            "qualifying_90_day": self.qualifying_90_day,
            "total": self.total,
            "unresolved": self.unresolved,
        }


@dataclass(frozen=True)
class DecisionStandings:
    evaluated: int
    unknown: int
    open: int
    unattributed: int

    @property
    def total(self) -> int:
        return self.evaluated + self.unknown + self.open + self.unattributed

    def as_dict(self) -> dict:
        return {
            "evaluated": self.evaluated,
            "open": self.open,
            "total": self.total,
            "unattributed": self.unattributed,
            "unknown": self.unknown,
        }


@dataclass(frozen=True)
class ReconstructionFailure:
    decision_event_id: str
    error: str
    detail: str

    def as_dict(self) -> dict:
        return {
            "decision_event_id": self.decision_event_id,
            "detail": self.detail,
            "error": self.error,
        }


@dataclass(frozen=True)
class RuleMatch:
    logic_version: str
    artifact_hash: str
    #: None when this artifact has no factor on the signal's key (`not applicable`).
    rule: str | None
    decisions: int
    matched: int

    @property
    def applicable(self) -> bool:
        return self.rule is not None

    def as_dict(self) -> dict:
        return {
            "artifact_hash": self.artifact_hash,
            "decisions": self.decisions,
            "logic_version": self.logic_version,
            "matched": self.matched,
            "rule": self.rule,
        }


@dataclass(frozen=True)
class SignalRow:
    id: str
    kind: str
    input_key: str
    predicate: str
    known_true: int
    known_false: int
    unavailable: int
    absent: int
    not_applicable: int
    reconstruction_failed: int
    input_available: int
    input_consumed: int
    historical_rule_matched: tuple[RuleMatch, ...]
    comparison: Comparison

    def as_dict(self) -> dict:
        return {
            "absent": self.absent,
            "comparison": self.comparison.as_dict(),
            "historical_rule_matched": [match.as_dict() for match in self.historical_rule_matched],
            "id": self.id,
            "input_available": self.input_available,
            "input_consumed": self.input_consumed,
            "input_key": self.input_key,
            "kind": self.kind,
            "known_false": self.known_false,
            "known_true": self.known_true,
            "not_applicable": self.not_applicable,
            "predicate": self.predicate,
            "reconstruction_failed": self.reconstruction_failed,
            "unavailable": self.unavailable,
        }


@dataclass(frozen=True)
class WorkflowRow:
    workflow_version: str
    decisions: int
    standings: DecisionStandings
    rate: Rate

    def as_dict(self) -> dict:
        return {
            "decisions": self.decisions,
            "rate": self.rate.as_dict(),
            "standings": self.standings.as_dict(),
            "workflow_version": self.workflow_version,
        }


@dataclass(frozen=True)
class WorkflowComparison:
    workflow_version: str
    comparison_workflow_version: str
    comparison: Comparison

    def as_dict(self) -> dict:
        return {
            "comparison": self.comparison.as_dict(),
            "comparison_workflow_version": self.comparison_workflow_version,
            "workflow_version": self.workflow_version,
        }


@dataclass(frozen=True)
class Insights:
    cutoff: int
    population: int
    reconstructed: int
    reconstruction_failures: tuple[ReconstructionFailure, ...]
    observations: ObservationCoverage
    standings: DecisionStandings
    overall: Rate
    signals: tuple[SignalRow, ...]
    workflows: tuple[WorkflowRow, ...]
    workflow_comparison: WorkflowComparison

    def as_dict(self) -> dict:
        return {
            "cutoff": self.cutoff,
            "observations": self.observations.as_dict(),
            "overall": self.overall.as_dict(),
            "population": self.population,
            "reconstructed": self.reconstructed,
            "reconstruction_failures": [f.as_dict() for f in self.reconstruction_failures],
            "signals": [row.as_dict() for row in self.signals],
            "standings": self.standings.as_dict(),
            "workflow_comparison": self.workflow_comparison.as_dict(),
            "workflows": [row.as_dict() for row in self.workflows],
        }


# --- Per-observation and per-decision facts --------------------------------------------


@dataclass(frozen=True)
class ObservationFact:
    """One effective outcome version with its effective attribution standing."""

    outcome_event_id: str
    account_ref: str
    schema_version: str
    standing: str
    state: str
    exactly_90_days: bool
    opportunity: bool | None
    resolved_action_event_id: str | None
    resolved_decision_event_id: str | None

    @property
    def attributed(self) -> bool:
        return self.standing in ATTRIBUTED_STATUSES

    @property
    def qualifying(self) -> bool:
        return self.attributed and self.state == STATE_CLOSED_KNOWN and self.exactly_90_days


@dataclass(frozen=True)
class DecisionFacts:
    """Everything the metrics read about one decision, at one cutoff.

    `observations` holds only the observations attributed to this decision.
    `context_states` and `historical_rules` are empty when reconstruction
    failed; `historical_rules` maps a signal id to the `(rule, matched)` of this
    decision's verified factor on the signal's key, or None when none exists.
    """

    decision_event_id: str
    account_ref: str
    workflow_version: str
    logic_version: str
    artifact_hash: str
    reconstruction_failure: ReconstructionFailure | None
    context_states: Mapping[str, str]
    available_inputs: frozenset[str]
    consumed_inputs: frozenset[str]
    observations: tuple[ObservationFact, ...]
    signal_states: Mapping[str, str]
    historical_rules: Mapping[str, tuple[str, bool] | None]

    @property
    def reconstructed(self) -> bool:
        return self.reconstruction_failure is None

    @property
    def standing(self) -> str:
        states = {observation.state for observation in self.observations}
        if STATE_CLOSED_KNOWN in states:
            return STANDING_EVALUATED
        if STATE_CLOSED_UNKNOWN in states:
            return STANDING_UNKNOWN
        if self.observations:
            return STANDING_OPEN
        return STANDING_UNATTRIBUTED

    @property
    def qualifying_observations(self) -> tuple[ObservationFact, ...]:
        return tuple(observation for observation in self.observations if observation.qualifying)

    @property
    def eligible(self) -> bool:
        return self.reconstructed and bool(self.qualifying_observations)

    @property
    def positive(self) -> bool:
        return self.eligible and any(o.opportunity is True for o in self.qualifying_observations)


# --- Reads ----------------------------------------------------------------------------


def _population_query(cutoff: int):
    prospects = accounts_query().subquery()
    return (
        select(
            decisions.c.decision_event_id,
            decisions.c.account_ref,
            decisions.c.workflow_version,
            decisions.c.logic_version,
            decisions.c.artifact_hash,
        )
        .join(prospects, prospects.c.account_ref == decisions.c.account_ref)
        .where(decisions.c.ingest_sequence <= cutoff)
        .where(decisions.c.decision_class == ATTRIBUTABLE_DECISION_CLASS)
        .order_by(decisions.c.ingest_sequence)
    )


def _period_is_exactly_90_days(row) -> bool:
    if row.schema_version == "1":
        return row.window_days == QUALIFYING_PERIOD_DAYS
    opened = datetime.fromisoformat(row.window_opened_at)
    closes = datetime.fromisoformat(row.window_closes_at)
    return closes - opened == QUALIFYING_PERIOD


def _state(row) -> str:
    if row.schema_version == "1":
        return STATE_CLOSED_KNOWN
    if row.evaluation_state == "open":
        return STATE_OPEN
    return STATE_CLOSED_KNOWN if row.opportunity is not None else STATE_CLOSED_UNKNOWN


def _outcome_query(cutoff: int, account_ref: str | None):
    query = (
        select(outcomes)
        .join(events, events.c.event_id == outcomes.c.outcome_event_id)
        .where(events.c.ingest_sequence <= cutoff)
        .order_by(events.c.ingest_sequence)
    )
    if account_ref is not None:
        query = query.where(outcomes.c.account_ref == account_ref)
    return query


def _effective_ids(conn, cutoff: int, account_ref: str | None) -> tuple[str, ...]:
    try:
        return effective_outcome_versions(conn, cutoff=cutoff, account_ref=account_ref)
    except AmbiguousSelection as error:
        # Name the chain: walk the versions in ingest order to the one that raises.
        for row in conn.execute(_outcome_query(cutoff, account_ref)).all():
            try:
                effective_outcome_version(conn, row.outcome_event_id, cutoff=cutoff)
            except AmbiguousSelection as named:
                raise SelectionFailure(row.outcome_event_id, named.candidates) from named
        raise SelectionFailure("", error.candidates) from error


def _observations(conn, cutoff: int, account_ref: str | None) -> tuple[ObservationFact, ...]:
    """(b) Effective outcome versions, then the effective attribution of each."""
    ids = _effective_ids(conn, cutoff, account_ref)
    rows = {row.outcome_event_id: row for row in conn.execute(_outcome_query(cutoff, account_ref))}
    facts = []
    for outcome_event_id in ids:
        try:
            result = effective_attribution(conn, outcome_event_id, POLICY_VERSION, cutoff=cutoff)
        except AmbiguousSelection as error:
            raise SelectionFailure(outcome_event_id, error.candidates) from error
        row = rows[outcome_event_id]
        facts.append(
            ObservationFact(
                outcome_event_id=outcome_event_id,
                account_ref=row.account_ref,
                schema_version=row.schema_version,
                standing=AWAITING_ATTRIBUTION if result is None else result.status,
                state=_state(row),
                exactly_90_days=_period_is_exactly_90_days(row),
                opportunity=row.opportunity,
                resolved_action_event_id=(
                    None if result is None else result.resolved_action_event_id
                ),
                resolved_decision_event_id=(
                    None if result is None else result.resolved_decision_event_id
                ),
            )
        )
    return tuple(facts)


def _contexts(conn, cutoff: int, decision_ids: Sequence[str]):
    """Preserved context availability and values, and consumed keys, per decision."""
    context: dict[str, dict[str, tuple[str, str | None]]] = {d: {} for d in decision_ids}
    for row in conn.execute(
        select(
            decision_context.c.decision_event_id,
            decision_context.c.input_key,
            decision_context.c.availability,
            decision_context.c.value_text,
        )
        .join(decisions, decisions.c.decision_event_id == decision_context.c.decision_event_id)
        .where(decisions.c.ingest_sequence <= cutoff)
    ):
        if row.decision_event_id in context:
            context[row.decision_event_id][row.input_key] = (row.availability, row.value_text)
    consumed: dict[str, set[str]] = {d: set() for d in decision_ids}
    for row in conn.execute(
        select(decision_consumed_inputs.c.decision_event_id, decision_consumed_inputs.c.input_key)
        .join(
            decisions,
            decisions.c.decision_event_id == decision_consumed_inputs.c.decision_event_id,
        )
        .where(decisions.c.ingest_sequence <= cutoff)
    ):
        if row.decision_event_id in consumed:
            consumed[row.decision_event_id].add(row.input_key)
    return context, consumed


def _rule_signal_state(signal: SignalDefinition, reconstruction) -> str:
    factor = next(
        (
            f
            for f in reconstruction.result.factors
            if f.key == signal.input_key and f.rule == signal.rule
        ),
        None,
    )
    if factor is None:
        return NOT_APPLICABLE
    if factor.input_state is InputState.ABSENT:
        return ABSENT
    if factor.input_state is InputState.UNAVAILABLE:
        return UNAVAILABLE
    return KNOWN_TRUE if factor.matched else KNOWN_FALSE


def _context_value_signal_state(
    signal: SignalDefinition, reconstruction, preserved: Mapping[str, tuple[str, str | None]]
) -> str:
    states = reconstruction.result.context_states
    # Membership, not `.get` with a default: an absent key is missing from the mapping.
    if signal.input_key not in states:
        return ABSENT
    if states[signal.input_key] is InputState.UNAVAILABLE:
        return UNAVAILABLE
    _, value_text = preserved[signal.input_key]
    return KNOWN_TRUE if same_scalar(json.loads(value_text), signal.equals) else KNOWN_FALSE


def _facts(
    conn, cutoff: int, signals: Sequence[SignalDefinition], *, decision_event_id: str | None = None
) -> tuple[list[DecisionFacts], tuple[ObservationFact, ...]]:
    validate_cutoff(conn, cutoff)

    # (a) The population.
    query = _population_query(cutoff)
    if decision_event_id is not None:
        query = query.where(decisions.c.decision_event_id == decision_event_id)
    population = conn.execute(query).all()
    if decision_event_id is not None and not population:
        raise LookupError(
            f"decision {decision_event_id!r} is not in the population at cutoff {cutoff}"
        )

    # (b) Effective observations, scoped to the account for a single decision.
    scope = population[0].account_ref if decision_event_id is not None else None
    observations = _observations(conn, cutoff, scope)

    # (c) Reconstruction.
    reconstructions = {}
    failures = {}
    for row in population:
        try:
            reconstructions[row.decision_event_id] = reconstruct(conn, row.decision_event_id)
        except REPLAY_FAILURES as error:
            failures[row.decision_event_id] = ReconstructionFailure(
                decision_event_id=row.decision_event_id,
                error=type(error).__name__,
                detail=str(error),
            )

    # (d) Attribution of observations to decisions.
    attributed: dict[str, list[ObservationFact]] = {row.decision_event_id: [] for row in population}
    for observation in observations:
        if observation.attributed and observation.resolved_decision_event_id in attributed:
            attributed[observation.resolved_decision_event_id].append(observation)

    # (e) Per-decision states.
    contexts, consumed = _contexts(conn, cutoff, [row.decision_event_id for row in population])
    facts = []
    for row in population:
        d = row.decision_event_id
        preserved = contexts[d]
        reconstruction = reconstructions.get(d)
        signal_states: dict[str, str] = {}
        historical_rules: dict[str, tuple[str, bool] | None] = {}
        for signal in signals:
            if reconstruction is None:
                signal_states[signal.id] = RECONSTRUCTION_FAILED
                continue
            if signal.kind == KIND_RULE:
                signal_states[signal.id] = _rule_signal_state(signal, reconstruction)
            else:
                signal_states[signal.id] = _context_value_signal_state(
                    signal, reconstruction, preserved
                )
            on_key = next(
                (f for f in reconstruction.result.factors if f.key == signal.input_key), None
            )
            historical_rules[signal.id] = None if on_key is None else (on_key.rule, on_key.matched)
        facts.append(
            DecisionFacts(
                decision_event_id=d,
                account_ref=row.account_ref,
                workflow_version=row.workflow_version,
                logic_version=reconstruction.logic_version if reconstruction else row.logic_version,
                artifact_hash=reconstruction.artifact_hash if reconstruction else row.artifact_hash,
                reconstruction_failure=failures.get(d),
                context_states=(
                    {key: str(state) for key, state in reconstruction.result.context_states.items()}
                    if reconstruction
                    else {}
                ),
                available_inputs=frozenset(
                    key
                    for key, (availability, _) in preserved.items()
                    if availability == "available"
                ),
                consumed_inputs=frozenset(consumed[d]),
                observations=tuple(attributed[d]),
                signal_states=signal_states,
                historical_rules=historical_rules,
            )
        )
    return facts, observations


# --- Aggregation ------------------------------------------------------------------------


def _rate(facts: Sequence[DecisionFacts]) -> Rate:
    return Rate(
        cohort_total=len(facts),
        eligible=sum(1 for f in facts if f.eligible),
        positives=sum(1 for f in facts if f.positive),
        excluded_other_period_only=sum(
            1 for f in facts if f.standing == STANDING_EVALUATED and not f.qualifying_observations
        ),
        excluded_reconstruction_failed=sum(1 for f in facts if not f.reconstructed),
        excluded_not_evaluated=sum(1 for f in facts if f.standing != STANDING_EVALUATED),
    )


def _standings(facts: Sequence[DecisionFacts]) -> DecisionStandings:
    counts = {
        STANDING_EVALUATED: 0,
        STANDING_UNKNOWN: 0,
        STANDING_OPEN: 0,
        STANDING_UNATTRIBUTED: 0,
    }
    for f in facts:
        counts[f.standing] += 1
    return DecisionStandings(
        evaluated=counts[STANDING_EVALUATED],
        unknown=counts[STANDING_UNKNOWN],
        open=counts[STANDING_OPEN],
        unattributed=counts[STANDING_UNATTRIBUTED],
    )


def _coverage(observations: Sequence[ObservationFact]) -> ObservationCoverage:
    def count(predicate) -> int:
        return sum(1 for o in observations if predicate(o))

    return ObservationCoverage(
        awaiting_attribution=count(lambda o: o.standing == AWAITING_ATTRIBUTION),
        unresolved=count(lambda o: o.standing == STATUS_UNRESOLVED),
        direct=count(lambda o: o.standing == STATUS_DIRECT),
        inferred=count(lambda o: o.standing == STATUS_INFERRED),
        open=count(lambda o: o.state == STATE_OPEN),
        closed_known=count(lambda o: o.state == STATE_CLOSED_KNOWN),
        closed_unknown=count(lambda o: o.state == STATE_CLOSED_UNKNOWN),
        other_period=count(lambda o: not o.exactly_90_days),
        qualifying_90_day=count(lambda o: o.qualifying),
        total=len(observations),
    )


def _signal_row(signal: SignalDefinition, facts: Sequence[DecisionFacts]) -> SignalRow:
    def in_state(state: str) -> list[DecisionFacts]:
        return [f for f in facts if f.signal_states[signal.id] == state]

    by_artifact: dict[tuple[str, str], list[DecisionFacts]] = {}
    for f in facts:
        if f.reconstructed:
            by_artifact.setdefault((f.logic_version, f.artifact_hash), []).append(f)
    matches = []
    for (logic_version, artifact_hash), members in sorted(by_artifact.items()):
        # The factor on the key is part of the artifact, so one member names it for all.
        on_key = members[0].historical_rules[signal.id]
        matches.append(
            RuleMatch(
                logic_version=logic_version,
                artifact_hash=artifact_hash,
                rule=None if on_key is None else on_key[0],
                decisions=len(members),
                matched=(
                    0
                    if on_key is None
                    else sum(1 for m in members if m.historical_rules[signal.id][1])
                ),
            )
        )
    return SignalRow(
        id=signal.id,
        kind=signal.kind,
        input_key=signal.input_key,
        predicate=signal.predicate,
        known_true=len(in_state(KNOWN_TRUE)),
        known_false=len(in_state(KNOWN_FALSE)),
        unavailable=len(in_state(UNAVAILABLE)),
        absent=len(in_state(ABSENT)),
        not_applicable=len(in_state(NOT_APPLICABLE)),
        reconstruction_failed=len(in_state(RECONSTRUCTION_FAILED)),
        input_available=sum(1 for f in facts if signal.input_key in f.available_inputs),
        input_consumed=sum(1 for f in facts if signal.input_key in f.consumed_inputs),
        historical_rule_matched=tuple(matches),
        comparison=Comparison(
            present=_rate(in_state(KNOWN_TRUE)), absent=_rate(in_state(KNOWN_FALSE))
        ),
    )


# --- Entry points ------------------------------------------------------------------------


def insights(
    conn,
    cutoff: int,
    *,
    signals: Sequence[SignalDefinition],
    comparison_workflow_version: str,
) -> Insights:
    """Every decision metric of D-014 at one cutoff. Reads only."""
    facts, observations = _facts(conn, cutoff, signals)
    versions = sorted({f.workflow_version for f in facts})

    def cohort(version: str) -> list[DecisionFacts]:
        return [f for f in facts if f.workflow_version == version]

    return Insights(
        cutoff=cutoff,
        population=len(facts),
        reconstructed=sum(1 for f in facts if f.reconstructed),
        reconstruction_failures=tuple(
            sorted(
                (f.reconstruction_failure for f in facts if not f.reconstructed),
                key=lambda failure: failure.decision_event_id,
            )
        ),
        observations=_coverage(observations),
        standings=_standings(facts),
        overall=_rate(facts),
        signals=tuple(_signal_row(signal, facts) for signal in signals),
        workflows=tuple(
            WorkflowRow(
                workflow_version=version,
                decisions=len(cohort(version)),
                standings=_standings(cohort(version)),
                rate=_rate(cohort(version)),
            )
            for version in versions
        ),
        workflow_comparison=WorkflowComparison(
            workflow_version=WORKFLOW_UNDER_COMPARISON,
            comparison_workflow_version=comparison_workflow_version,
            comparison=Comparison(
                present=_rate(cohort(WORKFLOW_UNDER_COMPARISON)),
                absent=_rate(cohort(comparison_workflow_version)),
            ),
        ),
    )


def decision_facts(
    conn, decision_event_id: str, cutoff: int, *, signals: Sequence[SignalDefinition]
) -> DecisionFacts:
    """The per-decision states `insights` aggregates, for one decision.

    Public so tests can assert a decision's membership rather than only a
    count. It follows the same definitions and order as `insights`, with the
    effective selections scoped to the decision's own account (the account
    every attribution to the decision is recorded under). Raises `LookupError`
    when the decision is not in the population at `cutoff`.
    """
    facts, _ = _facts(conn, cutoff, signals, decision_event_id=decision_event_id)
    return facts[0]
