"""`outcome-attribution-v1`: the deterministic attribution policy (D-007, D-013).

**Pure and read-only.** `attribute` reads the ledger through the narrow selects
below and writes nothing. The collector calls the same function to verify a
submitted `outcome.attributed` result, so the computation path and the
validation path are one implementation.

**The snapshot cutoff.** `cutoff` is an `events.ingest_sequence` value. Every
read -- the outcome version, its source claims, every action and decision, and
every lookup that decides a reason -- joins the projected row to the event that
produced it and keeps only rows whose `ingest_sequence` is at or below the
cutoff. The policy reads no evidence and no attribution rows, and it never
reads the `*_unusable_reason` columns an outcome row recorded at ingest; it
recomputes every reason inside the snapshot. A result is therefore reproducible
after any later append.

**The observation instant.** Every temporal rule compares against one instant:
a v2 outcome's `observed_at`, or a v1 outcome's envelope `occurred_at`.

**Source claims by schema version.** A v2 outcome's claims are its
`source_action_event_id` and `source_decision_event_id`. A v1 outcome's stored
`action_event_id` is mapped into the same evaluation as a source action claim,
here and never at ingest, so a v1 outcome naming a failed or long-past action
resolves exactly as the equivalent v2 claim would.

**Resolution, exactly D-007.**

1. A valid explicit source reference is `direct`. A decision claim alone is
   valid when it names a recorded `account_prioritization` decision for the
   same account whose boundary is strictly before the observation instant. An
   action claim alone is valid when the action is eligible. When both are
   supplied, both must be valid and the action's own `decision_event_id` must
   be the claimed decision; a valid decision paired with an ineligible action
   is not salvaged into decision-only credit.
2. Otherwise the most recent eligible action is `inferred`, with a heuristic
   `method`.
3. Otherwise `unresolved`.

An action is **eligible** when all hold: same account; status `sent` or
`completed`; linked to a recorded `account_prioritization` decision for the
same account; occurred strictly before the observation instant; occurred no
more than `LOOKBACK_DAYS * 24` hours before it (inclusive); recorded at or
before the cutoff. Eligible actions at the same occurrence instant are ordered
by `events.ingest_sequence`, highest first, independent of iteration order.

**Reasons.** A `direct` reason names the valid reference shape. A fallback
reason is `<claims>;<resolution>`: `<claims>` is `no_source_reference` or the
unusable-claim reasons joined by `+`, and `<resolution>` is
`most_recent_eligible_action` or `no_eligible_action`.

**Selection.** `effective_outcome_version` and `effective_attribution` are the
two selection operations every reader uses: resolve the outcome version first,
then the attribution of that exact version. A corrected outcome does not
inherit its predecessor's result. Both raise rather than pick when a snapshot
holds two candidates, and neither caches anything.
"""

from collections.abc import Iterable
from dataclasses import dataclass, fields
from datetime import datetime, timedelta

from sqlalchemy import and_, func, select

from flight_recorder.collector.canonical import canonical_hash
from flight_recorder.ledger.schema import (
    actions,
    decisions,
    events,
    outcome_attributions,
    outcomes,
)

POLICY_VERSION = "outcome-attribution-v1"
IMPLEMENTED_POLICY_VERSIONS = frozenset({POLICY_VERSION})

#: The policy's attribution lookback. It is not an outcome's evaluation period.
LOOKBACK_DAYS = 90
LOOKBACK = timedelta(hours=LOOKBACK_DAYS * 24)

ELIGIBLE_ACTION_STATUSES = frozenset({"sent", "completed"})
ATTRIBUTABLE_DECISION_CLASS = "account_prioritization"

STATUS_DIRECT = "direct"
STATUS_INFERRED = "inferred"
STATUS_UNRESOLVED = "unresolved"

METHOD_EXPLICIT_REFERENCE = "explicit_source_reference"
METHOD_HEURISTIC = "heuristic_most_recent_eligible_action"
METHOD_UNRESOLVED = "no_resolution"
HEURISTIC_METHODS = frozenset({METHOD_HEURISTIC})

# --- The reason vocabulary ----------------------------------------------------

VALID_SOURCE_ACTION = "valid_source_action_reference"
VALID_SOURCE_DECISION = "valid_source_decision_reference"
VALID_SOURCE_ACTION_AND_DECISION = "valid_source_action_and_decision_references"

NO_SOURCE_REFERENCE = "no_source_reference"

#: Why a source action claim is unusable, in the order the checks run.
SOURCE_ACTION_NOT_RECORDED_BY_CUTOFF = "source_action_not_recorded_by_cutoff"
SOURCE_ACTION_OTHER_ACCOUNT = "source_action_other_account"
SOURCE_ACTION_FAILED = "source_action_failed"
SOURCE_ACTION_NOT_LINKED_TO_PRIORITIZATION_DECISION = (
    "source_action_not_linked_to_prioritization_decision"
)
SOURCE_ACTION_NOT_BEFORE_OBSERVATION = "source_action_not_before_observation"
SOURCE_ACTION_OUTSIDE_LOOKBACK = "source_action_outside_lookback"

#: Why a source decision claim is unusable, in the order the checks run.
SOURCE_DECISION_NOT_RECORDED_BY_CUTOFF = "source_decision_not_recorded_by_cutoff"
SOURCE_DECISION_OTHER_ACCOUNT = "source_decision_other_account"
SOURCE_DECISION_NOT_ACCOUNT_PRIORITIZATION = "source_decision_not_account_prioritization"
SOURCE_DECISION_NOT_BEFORE_OBSERVATION = "source_decision_not_before_observation"

#: Both claims are individually valid but the action names a different decision.
SOURCE_REFERENCES_DISAGREE = "source_references_disagree"

MOST_RECENT_ELIGIBLE_ACTION = "most_recent_eligible_action"
NO_ELIGIBLE_ACTION = "no_eligible_action"

CLAIM_SEPARATOR = "+"
SEGMENT_SEPARATOR = ";"


# --- Named failures -------------------------------------------------------------


class AttributionError(Exception):
    """A named failure. It is never a result: no failure becomes `unresolved`."""

    reason = "attribution_failure"


class UnsupportedPolicyVersion(AttributionError):
    reason = "unsupported_policy_version"

    def __init__(self, policy_version: str):
        super().__init__(
            f"policy_version {policy_version!r} is not implemented; implemented: "
            + ", ".join(sorted(IMPLEMENTED_POLICY_VERSIONS))
        )
        self.policy_version = policy_version


class InvalidCutoff(AttributionError):
    reason = "ingest_cutoff_is_not_an_integer"

    def __init__(self, cutoff):
        super().__init__(f"ingest_cutoff {cutoff!r} is not an integer ingest sequence")
        self.cutoff = cutoff


class CutoffAboveMaximum(AttributionError):
    reason = "ingest_cutoff_above_ledger_maximum"

    def __init__(self, cutoff: int, maximum: int | None):
        super().__init__(
            f"ingest_cutoff {cutoff} is greater than the ledger's maximum ingest sequence {maximum}"
        )
        self.cutoff = cutoff
        self.maximum = maximum


class CutoffNotFound(AttributionError):
    reason = "ingest_cutoff_not_found"

    def __init__(self, cutoff: int):
        super().__init__(f"ingest_cutoff {cutoff} names no recorded ingest sequence")
        self.cutoff = cutoff


class OutcomeNotFound(AttributionError):
    reason = "unknown_outcome_event_id"

    def __init__(self, outcome_event_id: str):
        super().__init__(f"outcome_event_id {outcome_event_id!r} is not a recorded outcome")
        self.outcome_event_id = outcome_event_id


class CutoffExcludesOutcome(AttributionError):
    reason = "ingest_cutoff_excludes_the_outcome"

    def __init__(self, outcome_event_id: str, cutoff: int, outcome_sequence: int):
        super().__init__(
            f"ingest_cutoff {cutoff} is before outcome {outcome_event_id!r}, recorded at "
            f"ingest sequence {outcome_sequence}; an outcome cannot be attributed in a "
            "snapshot that does not contain it"
        )
        self.outcome_event_id = outcome_event_id
        self.cutoff = cutoff
        self.outcome_sequence = outcome_sequence


class AmbiguousSelection(AttributionError):
    reason = "ambiguous_effective_selection"

    def __init__(self, what: str, candidates: Iterable[str]):
        listed = sorted(candidates)
        super().__init__(f"{what}: more than one effective candidate: {', '.join(listed)}")
        self.what = what
        self.candidates = tuple(listed)


# --- Records --------------------------------------------------------------------

#: The fields that make two results "the same result". Cutoff, event identity,
#: timestamps and supersession metadata are deliberately excluded.
POLICY_RESULT_FIELDS = (
    "outcome_event_id",
    "policy_version",
    "method",
    "window_days",
    "resolved_action_event_id",
    "resolved_decision_event_id",
    "status",
    "reason",
)


@dataclass(frozen=True)
class AttributionResult:
    outcome_event_id: str
    policy_version: str
    method: str
    window_days: int
    resolved_action_event_id: str | None
    resolved_decision_event_id: str | None
    status: str
    reason: str
    cutoff: int

    @property
    def heuristic(self) -> bool:
        return self.method in HEURISTIC_METHODS


@dataclass(frozen=True)
class StoredAttribution:
    """One `outcome_attributions` row."""

    attribution_event_id: str
    account_ref: str
    source_event_id: str
    outcome_event_id: str
    policy_version: str
    method: str
    window_days: int
    resolved_action_event_id: str | None
    resolved_decision_event_id: str | None
    status: str
    reason: str
    attributed_at: str
    ingest_cutoff: int
    supersedes_attribution_event_id: str | None

    @property
    def heuristic(self) -> bool:
        return self.method in HEURISTIC_METHODS


def policy_result(record) -> tuple:
    """The policy-result fields of a result, a stored row, or a payload dict."""
    if isinstance(record, dict):
        return tuple(record[name] for name in POLICY_RESULT_FIELDS)
    return tuple(getattr(record, name) for name in POLICY_RESULT_FIELDS)


def same_policy_result(left, right) -> bool:
    """True when two results agree on every policy-result field and no other."""
    return policy_result(left) == policy_result(right)


@dataclass(frozen=True)
class OutcomeClaims:
    outcome_event_id: str
    account_ref: str
    schema_version: str
    observation_instant: str
    source_action_event_id: str | None
    source_decision_event_id: str | None
    ingest_sequence: int


@dataclass(frozen=True)
class ActionCandidate:
    """An action as the snapshot records it, with its decision when recorded."""

    action_event_id: str
    account_ref: str
    decision_event_id: str
    status: str
    occurred_at: str
    ingest_sequence: int
    #: None when the linked decision is not in the snapshot.
    decision_class: str | None
    decision_account_ref: str | None


@dataclass(frozen=True)
class DecisionClaim:
    decision_event_id: str
    account_ref: str
    decision_class: str
    decision_boundary: str


# --- Operation identity ---------------------------------------------------------

ATTRIBUTION_EVENT_ID_PREFIX = "evt-attribution-"


def attribution_event_id(outcome_event_id: str, policy_version: str, ingest_cutoff: int) -> str:
    """The event id of one attribution operation, derived and never chosen.

    `evt-attribution-` followed by the SHA-256 of the canonical JSON of
    `{"ingest_cutoff", "outcome_event_id", "policy_version"}`. The attribution
    time is not an input, so a retry of the operation keeps its identity.
    """
    digest = canonical_hash(
        {
            "outcome_event_id": outcome_event_id,
            "policy_version": policy_version,
            "ingest_cutoff": ingest_cutoff,
        }
    )
    return f"{ATTRIBUTION_EVENT_ID_PREFIX}{digest}"


# --- Time -----------------------------------------------------------------------


def instant(text: str) -> datetime:
    """A stored normalized timestamp as an aware datetime."""
    return datetime.fromisoformat(text)


# --- Snapshot reads ---------------------------------------------------------------


def ledger_maximum(conn) -> int | None:
    """The collector's current maximum `ingest_sequence`, or None when empty."""
    return conn.execute(select(func.max(events.c.ingest_sequence))).scalar_one()


def validate_cutoff(conn, cutoff) -> int:
    """A usable snapshot boundary, or a named failure."""
    if type(cutoff) is not int:
        raise InvalidCutoff(cutoff)
    maximum = ledger_maximum(conn)
    if maximum is None or cutoff > maximum:
        raise CutoffAboveMaximum(cutoff, maximum)
    found = conn.execute(
        select(events.c.ingest_sequence).where(events.c.ingest_sequence == cutoff)
    ).first()
    if found is None:
        raise CutoffNotFound(cutoff)
    return cutoff


def load_outcome(conn, outcome_event_id: str, *, cutoff: int) -> OutcomeClaims:
    """The outcome version and its claims, which must be inside the snapshot.

    The outcome's own ingest sequence is read even when it lies beyond the
    cutoff, solely to name that failure; nothing beyond the cutoff decides a
    result.
    """
    row = conn.execute(
        select(outcomes, events.c.ingest_sequence)
        .join(events, events.c.event_id == outcomes.c.outcome_event_id)
        .where(outcomes.c.outcome_event_id == outcome_event_id)
    ).first()
    if row is None:
        raise OutcomeNotFound(outcome_event_id)
    if row.ingest_sequence > cutoff:
        raise CutoffExcludesOutcome(outcome_event_id, cutoff, row.ingest_sequence)
    if row.schema_version == "1":
        return OutcomeClaims(
            outcome_event_id=row.outcome_event_id,
            account_ref=row.account_ref,
            schema_version=row.schema_version,
            observation_instant=row.occurred_at,
            source_action_event_id=row.action_event_id,
            source_decision_event_id=None,
            ingest_sequence=row.ingest_sequence,
        )
    return OutcomeClaims(
        outcome_event_id=row.outcome_event_id,
        account_ref=row.account_ref,
        schema_version=row.schema_version,
        observation_instant=row.observed_at,
        source_action_event_id=row.source_action_event_id,
        source_decision_event_id=row.source_decision_event_id,
        ingest_sequence=row.ingest_sequence,
    )


def _action_query(cutoff: int):
    action_event = events.alias("action_event")
    decision_event = events.alias("decision_event")
    return (
        select(
            actions.c.action_event_id,
            actions.c.account_ref,
            actions.c.decision_event_id,
            actions.c.status,
            actions.c.occurred_at,
            action_event.c.ingest_sequence,
            decisions.c.decision_class,
            decisions.c.account_ref.label("decision_account_ref"),
        )
        .select_from(
            actions.join(action_event, action_event.c.event_id == actions.c.action_event_id)
            .outerjoin(
                decision_event,
                and_(
                    decision_event.c.event_id == actions.c.decision_event_id,
                    decision_event.c.ingest_sequence <= cutoff,
                ),
            )
            .outerjoin(decisions, decisions.c.decision_event_id == decision_event.c.event_id)
        )
        .where(action_event.c.ingest_sequence <= cutoff)
    )


def _candidate(row) -> ActionCandidate:
    return ActionCandidate(
        action_event_id=row.action_event_id,
        account_ref=row.account_ref,
        decision_event_id=row.decision_event_id,
        status=row.status,
        occurred_at=row.occurred_at,
        ingest_sequence=row.ingest_sequence,
        decision_class=row.decision_class,
        decision_account_ref=row.decision_account_ref,
    )


def load_action(conn, action_event_id: str, *, cutoff: int) -> ActionCandidate | None:
    row = conn.execute(
        _action_query(cutoff).where(actions.c.action_event_id == action_event_id)
    ).first()
    return None if row is None else _candidate(row)


def load_account_actions(conn, account_ref: str, *, cutoff: int) -> tuple[ActionCandidate, ...]:
    """Every action recorded for the account by the cutoff, in no promised order."""
    return tuple(
        _candidate(row)
        for row in conn.execute(_action_query(cutoff).where(actions.c.account_ref == account_ref))
    )


def load_decision(conn, decision_event_id: str, *, cutoff: int) -> DecisionClaim | None:
    row = conn.execute(
        select(
            decisions.c.decision_event_id,
            decisions.c.account_ref,
            decisions.c.decision_class,
            decisions.c.decision_boundary,
        )
        .join(events, events.c.event_id == decisions.c.decision_event_id)
        .where(decisions.c.decision_event_id == decision_event_id)
        .where(events.c.ingest_sequence <= cutoff)
    ).first()
    if row is None:
        return None
    return DecisionClaim(
        decision_event_id=row.decision_event_id,
        account_ref=row.account_ref,
        decision_class=row.decision_class,
        decision_boundary=row.decision_boundary,
    )


# --- The rules ------------------------------------------------------------------


def action_problem(
    action: ActionCandidate | None, *, account_ref: str, observation_instant: str
) -> str | None:
    """Why an action is not eligible, or None when it is.

    `action` is None when no such action is recorded by the cutoff, which
    covers both an identifier that never resolved and one recorded later: the
    snapshot cannot tell them apart without reading past its own boundary.
    """
    if action is None:
        return SOURCE_ACTION_NOT_RECORDED_BY_CUTOFF
    if action.account_ref != account_ref:
        return SOURCE_ACTION_OTHER_ACCOUNT
    if action.status not in ELIGIBLE_ACTION_STATUSES:
        return SOURCE_ACTION_FAILED
    if (
        action.decision_class != ATTRIBUTABLE_DECISION_CLASS
        or action.decision_account_ref != account_ref
    ):
        return SOURCE_ACTION_NOT_LINKED_TO_PRIORITIZATION_DECISION
    observed = instant(observation_instant)
    occurred = instant(action.occurred_at)
    if not occurred < observed:
        return SOURCE_ACTION_NOT_BEFORE_OBSERVATION
    if observed - occurred > LOOKBACK:
        return SOURCE_ACTION_OUTSIDE_LOOKBACK
    return None


def decision_problem(
    decision: DecisionClaim | None, *, account_ref: str, observation_instant: str
) -> str | None:
    """Why a decision claim cannot resolve directly, or None when it can."""
    if decision is None:
        return SOURCE_DECISION_NOT_RECORDED_BY_CUTOFF
    if decision.account_ref != account_ref:
        return SOURCE_DECISION_OTHER_ACCOUNT
    if decision.decision_class != ATTRIBUTABLE_DECISION_CLASS:
        return SOURCE_DECISION_NOT_ACCOUNT_PRIORITIZATION
    if not instant(decision.decision_boundary) < instant(observation_instant):
        return SOURCE_DECISION_NOT_BEFORE_OBSERVATION
    return None


def most_recent_eligible(candidates: Iterable[ActionCandidate]) -> ActionCandidate | None:
    """The latest occurrence instant, ties broken by the higher ingest sequence.

    The key is total over eligible actions (ingest sequences are unique), so
    the answer does not depend on the order candidates arrive in.
    """
    return max(
        candidates,
        key=lambda c: (instant(c.occurred_at), c.ingest_sequence),
        default=None,
    )


def attribute(
    conn, outcome_event_id: str, *, cutoff: int, policy_version: str = POLICY_VERSION
) -> AttributionResult:
    """`outcome-attribution-v1` for one outcome version at one snapshot cutoff."""
    if policy_version not in IMPLEMENTED_POLICY_VERSIONS:
        raise UnsupportedPolicyVersion(policy_version)
    validate_cutoff(conn, cutoff)
    outcome = load_outcome(conn, outcome_event_id, cutoff=cutoff)
    account_ref = outcome.account_ref
    observed = outcome.observation_instant

    def result(method, status, reason, action_id=None, decision_id=None) -> AttributionResult:
        return AttributionResult(
            outcome_event_id=outcome_event_id,
            policy_version=policy_version,
            method=method,
            window_days=LOOKBACK_DAYS,
            resolved_action_event_id=action_id,
            resolved_decision_event_id=decision_id,
            status=status,
            reason=reason,
            cutoff=cutoff,
        )

    action_claim = outcome.source_action_event_id
    decision_claim = outcome.source_decision_event_id
    problems: list[str] = []
    action = decision = None
    if action_claim is not None:
        action = load_action(conn, action_claim, cutoff=cutoff)
        problem = action_problem(action, account_ref=account_ref, observation_instant=observed)
        if problem is not None:
            problems.append(problem)
    if decision_claim is not None:
        decision = load_decision(conn, decision_claim, cutoff=cutoff)
        problem = decision_problem(decision, account_ref=account_ref, observation_instant=observed)
        if problem is not None:
            problems.append(problem)

    # 1. A valid explicit source reference.
    if (action_claim is not None or decision_claim is not None) and not problems:
        if action is not None and decision is not None:
            if action.decision_event_id == decision.decision_event_id:
                return result(
                    METHOD_EXPLICIT_REFERENCE,
                    STATUS_DIRECT,
                    VALID_SOURCE_ACTION_AND_DECISION,
                    action.action_event_id,
                    decision.decision_event_id,
                )
            problems.append(SOURCE_REFERENCES_DISAGREE)
        elif action is not None:
            return result(
                METHOD_EXPLICIT_REFERENCE,
                STATUS_DIRECT,
                VALID_SOURCE_ACTION,
                action.action_event_id,
                action.decision_event_id,
            )
        else:
            return result(
                METHOD_EXPLICIT_REFERENCE,
                STATUS_DIRECT,
                VALID_SOURCE_DECISION,
                None,
                decision.decision_event_id,
            )

    claims = CLAIM_SEPARATOR.join(problems) if problems else NO_SOURCE_REFERENCE

    # 2. The most recent eligible action, heuristically.
    chosen = most_recent_eligible(
        candidate
        for candidate in load_account_actions(conn, account_ref, cutoff=cutoff)
        if action_problem(candidate, account_ref=account_ref, observation_instant=observed) is None
    )
    if chosen is not None:
        return result(
            METHOD_HEURISTIC,
            STATUS_INFERRED,
            f"{claims}{SEGMENT_SEPARATOR}{MOST_RECENT_ELIGIBLE_ACTION}",
            chosen.action_event_id,
            chosen.decision_event_id,
        )

    # 3. Unresolved.
    return result(
        METHOD_UNRESOLVED,
        STATUS_UNRESOLVED,
        f"{claims}{SEGMENT_SEPARATOR}{NO_ELIGIBLE_ACTION}",
    )


def source_claim_problems(
    conn,
    *,
    account_ref: str,
    observation_instant: str,
    source_action_event_id: str | None,
    source_decision_event_id: str | None,
    cutoff: int,
) -> tuple[str | None, str | None]:
    """Each claim's own unusable reason at `cutoff`, or None when usable.

    Used at ingest to retain a reason beside each claim. It judges each claim
    alone; whether a pair agrees is decided only by `attribute`.
    """
    action_reason = decision_reason = None
    if source_action_event_id is not None:
        action_reason = action_problem(
            load_action(conn, source_action_event_id, cutoff=cutoff),
            account_ref=account_ref,
            observation_instant=observation_instant,
        )
    if source_decision_event_id is not None:
        decision_reason = decision_problem(
            load_decision(conn, source_decision_event_id, cutoff=cutoff),
            account_ref=account_ref,
            observation_instant=observation_instant,
        )
    return action_reason, decision_reason


# --- Selection ------------------------------------------------------------------


def effective_outcome_version(conn, outcome_chain: str, *, cutoff: int) -> str:
    """The version of `outcome_chain`'s supersession chain that nothing supersedes.

    `outcome_chain` is any outcome event id in the chain; the chain is followed
    forward through versions recorded by the cutoff. Two versions superseding
    the same version raise `AmbiguousSelection`.
    """
    load_outcome(conn, outcome_chain, cutoff=cutoff)
    current, seen = outcome_chain, {outcome_chain}
    while True:
        successors = [
            row.outcome_event_id
            for row in conn.execute(
                select(outcomes.c.outcome_event_id)
                .join(events, events.c.event_id == outcomes.c.outcome_event_id)
                .where(outcomes.c.supersedes_outcome_event_id == current)
                .where(events.c.ingest_sequence <= cutoff)
            )
        ]
        if len(successors) > 1:
            raise AmbiguousSelection(f"outcome version superseding {current!r}", successors)
        if not successors:
            return current
        current = successors[0]
        if current in seen:
            raise AmbiguousSelection("outcome supersession cycle", seen)
        seen.add(current)


def effective_outcome_versions(
    conn, *, cutoff: int, account_ref: str | None = None
) -> tuple[str, ...]:
    """Every effective outcome version recorded by the cutoff, once each.

    Built from `effective_outcome_version`, in ingest order of the versions.
    """
    query = (
        select(outcomes.c.outcome_event_id)
        .join(events, events.c.event_id == outcomes.c.outcome_event_id)
        .where(events.c.ingest_sequence <= cutoff)
        .order_by(events.c.ingest_sequence)
    )
    if account_ref is not None:
        query = query.where(outcomes.c.account_ref == account_ref)
    effective: dict[str, None] = {}
    for row in conn.execute(query).all():
        effective.setdefault(effective_outcome_version(conn, row.outcome_event_id, cutoff=cutoff))
    return tuple(effective)


_STORED_COLUMNS = tuple(f.name for f in fields(StoredAttribution))


def effective_attribution(
    conn, outcome_event_id: str, policy_version: str, *, cutoff: int
) -> StoredAttribution | None:
    """The attribution of *this exact* outcome version and policy that nothing
    supersedes, as of the cutoff, or None when that version has no result.

    A predecessor version's result is never returned for its successor.
    """
    rows = conn.execute(
        select(*(outcome_attributions.c[name] for name in _STORED_COLUMNS))
        .join(events, events.c.event_id == outcome_attributions.c.attribution_event_id)
        .where(outcome_attributions.c.outcome_event_id == outcome_event_id)
        .where(outcome_attributions.c.policy_version == policy_version)
        .where(events.c.ingest_sequence <= cutoff)
    ).all()
    superseded = {
        row.supersedes_attribution_event_id
        for row in rows
        if row.supersedes_attribution_event_id is not None
    }
    tips = [row for row in rows if row.attribution_event_id not in superseded]
    if len(tips) > 1:
        raise AmbiguousSelection(
            f"attribution of outcome {outcome_event_id!r} under {policy_version!r}",
            (row.attribution_event_id for row in tips),
        )
    if not tips:
        return None
    return StoredAttribution(**{name: getattr(tips[0], name) for name in _STORED_COLUMNS})
