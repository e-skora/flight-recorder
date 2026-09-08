"""The counterfactual: `R(Lc, H(d))`, and its comparison with the original.

This is the second of the only two valid replay questions in `PRODUCT.md` §5:
*what would an explicitly selected current artifact have decided over the
same sealed historical context?* It is a computation about a decision that did
not occur, and everything here is shaped so it can never be presented as one
(§4.5, INV-06, INV-10).

Verified before anything is evaluated, in this order, stopping at the first
failure:

1. the original, `R(Lh(d), H(d))`, through `reconstruct`: the decision's own
   artifact verified (steps 2 to 8), its context read with every preserved
   reference's availability checked against the stored boundary (step 9), and
   the result required to equal the record. A decision whose original cannot
   be reproduced exactly gets no counterfactual; the `ReconstructionError`
   propagates unchanged (§5 "Replay honesty": never fabricated);
2. the current artifact, by the hash the caller selected, through
   `verify_artifact_row`: registered, decodable, hashing to its own registered
   hash, strict schema, row identity, and written for this runtime's
   evaluator. Only the decision-identity comparison (step 7) is skipped,
   because a current artifact legitimately differs from the decision on
   `logic_version`;
3. the current artifact's `decision_class` equals the decision's.

What is read: the same `H(d)`, through the same `load_context`, with the same
stored boundary text, so every preserved reference is verified exactly as for
the original. Nothing else. Never `events`, never `accounts`, never any
evidence row other than the ids `H(d)` preserves, never the current account
state, never "the latest" artifact (INV-01, INV-02, INV-03).

Nothing is written. No counterfactual is persisted anywhere -- no table, no
column, no event, no cache (D-011, INV-06). The value returned is a
`Counterfactual`, a type with no relation to `Reconstruction`, carrying the
fixed label `"counterfactual"` on the value and the re-verified original
inside it, so the two sides are always both present and always told apart by
type and by text, never by position or color (`PRODUCT.md` §11).

`compare` turns one counterfactual into the data §4.5 requires: both logic
identities, both scores, thresholds and outputs, every contribution change
with its evidence reference and input state on each side, and every input
current logic expects that `H(d)` lacks, with `unavailable` and `absent` kept
distinct. It explains the whole difference or it is wrong:
`score_delta == sum(contribution_delta)` holds by construction.
"""

from dataclasses import dataclass
from datetime import datetime

from flight_recorder.logic.evaluator import (
    EvaluationResult,
    FactorResult,
    InputState,
    evaluate,
)
from flight_recorder.replay.reconstruct import (
    IntegrityFailure,
    Reconstruction,
    load_artifact_row,
    load_context,
    load_decision_row,
    reconstruct,
    verify_artifact_row,
)

__all__ = [
    "COUNTERFACTUAL_LABEL",
    "ORIGINAL_LABEL",
    "Comparison",
    "ContributionChange",
    "Counterfactual",
    "LogicIdentity",
    "MissingInput",
    "compare",
    "replay",
]

#: The labels the comparison carries and the label field on the result. A
#: later UI renders them as text, never as color alone (`PRODUCT.md` §11).
ORIGINAL_LABEL = "original"
COUNTERFACTUAL_LABEL = "counterfactual"


@dataclass(frozen=True)
class Counterfactual:
    """`R(Lc, H(d))` for one recorded decision, with the original beside it.

    Not a `Reconstruction` and not related to one by inheritance: a
    counterfactual is a different kind of thing from a decision that occurred.
    The numbers live under `result`, exactly as on `Reconstruction`, so a
    caller must name which side it reads.
    """

    #: Always `COUNTERFACTUAL_LABEL`; a field, so the label is visible on the
    #: value, and validated, so it cannot be forged.
    label: str
    decision_event_id: str
    #: `T(d)`, UTC-aware, the original's.
    decision_boundary: datetime
    #: The re-verified original, exactly as `reconstruct` returned it.
    original: Reconstruction
    current_artifact_hash: str
    current_logic_version: str
    current_evaluator_version: str
    result: EvaluationResult
    #: What verification of the current artifact established, before any
    #: evaluation ran (mirrors `Reconstruction`).
    stored_artifact_hash: str
    recomputed_artifact_hash: str
    runtime_evaluator_version: str

    def __post_init__(self) -> None:
        if self.label != COUNTERFACTUAL_LABEL:
            raise ValueError(
                f"a Counterfactual is always labeled {COUNTERFACTUAL_LABEL!r}; got {self.label!r}"
            )


def replay(conn, decision_event_id: str, current_artifact_hash: str) -> Counterfactual:
    """Evaluate the explicitly selected current artifact over the sealed `H(d)`.

    Raises rather than returning anything when the original cannot be
    reproduced exactly or the current artifact fails verification. Writes
    nothing; persists nothing; reads no `events` and no `accounts` row.
    """
    # 1. The original must reproduce exactly; any ReconstructionError propagates.
    original = reconstruct(conn, decision_event_id)
    decision_row = load_decision_row(conn, decision_event_id)

    # 2. The current artifact, verified by the same checks minus decision identity.
    current_row = load_artifact_row(conn, current_artifact_hash)
    current = verify_artifact_row(current_row, current_artifact_hash)

    # 3. Never replay a decision against logic for another decision class.
    if current.artifact.decision_class != decision_row.decision_class:
        raise IntegrityFailure(
            "decision_class",
            f"the decision is {decision_row.decision_class!r}, but the selected current "
            f"artifact is for {current.artifact.decision_class!r}",
            stored=decision_row.decision_class,
            recomputed=current.artifact.decision_class,
        )

    # 4. The same sealed context, through the same code path, same boundary text.
    context = load_context(conn, decision_event_id, decision_row.decision_boundary)

    # 5. Evaluate. There is no record to compare with: this decision never occurred.
    result = evaluate(current.artifact, context, original.decision_boundary)

    return Counterfactual(
        label=COUNTERFACTUAL_LABEL,
        decision_event_id=decision_event_id,
        decision_boundary=original.decision_boundary,
        original=original,
        current_artifact_hash=current.recomputed_artifact_hash,
        current_logic_version=current.artifact.logic_version,
        current_evaluator_version=current.artifact.evaluator_version,
        result=result,
        stored_artifact_hash=current.stored_artifact_hash,
        recomputed_artifact_hash=current.recomputed_artifact_hash,
        runtime_evaluator_version=current.runtime_evaluator_version,
    )


# --- The structured comparison (`PRODUCT.md` §4.5) ---------------------------


@dataclass(frozen=True)
class LogicIdentity:
    logic_version: str
    artifact_hash: str
    evaluator_version: str


@dataclass(frozen=True)
class ContributionChange:
    """One input key, as the historical and the current artifact treated it.

    `change` is one of exactly five, decided in this order:

    - `added`: only the current artifact has a factor for the key;
    - `removed`: only the historical artifact has one;
    - `unchanged`: both, with the same rule text, weight, input state, match
      and contribution;
    - `reweighted`: both, same rule text, different weight, same input state
      and match;
    - `changed`: both, anything else -- a different rule text, a different
      match, or a different input state.

    Classification follows the artifact text, not the outcome: a factor whose
    rule text changed is `changed` even when its contribution did not.
    """

    key: str
    #: The id `H(d)` preserves for this key, from whichever side consumed it;
    #: None when the input is `unavailable` or `absent`.
    evidence_version_id: str | None
    original: FactorResult | None
    counterfactual: FactorResult | None
    #: The key's state under each artifact: the factor's own state when it is a
    #: factor, else that side's `context_states` entry, else None.
    original_state: InputState | None
    counterfactual_state: InputState | None
    original_contribution: int
    counterfactual_contribution: int
    contribution_delta: int
    change: str


@dataclass(frozen=True)
class MissingInput:
    """An input the current artifact expects and `H(d)` lacks."""

    key: str
    #: `UNAVAILABLE` (explicitly recorded as unavailable at `T(d)`) or `ABSENT`
    #: (no entry in `H(d)` at all); never blurred (INV-03, INV-09).
    state: InputState


@dataclass(frozen=True)
class Comparison:
    decision_event_id: str
    decision_boundary: datetime
    original_label: str
    counterfactual_label: str
    historical_logic: LogicIdentity
    current_logic: LogicIdentity
    original_score: int
    counterfactual_score: int
    score_delta: int
    original_threshold: int
    counterfactual_threshold: int
    original_output: str
    counterfactual_output: str
    output_changed: bool
    contributions: tuple[ContributionChange, ...]
    missing_inputs: tuple[MissingInput, ...]
    ignored_by_historical: tuple[str, ...]
    ignored_by_current: tuple[str, ...]


_MISSING = (InputState.UNAVAILABLE, InputState.ABSENT)


def _classify(original: FactorResult | None, counterfactual: FactorResult | None) -> str:
    if original is None:
        return "added"
    if counterfactual is None:
        return "removed"
    same_rule = original.rule == counterfactual.rule
    same_verdict = (
        original.input_state is counterfactual.input_state
        and original.matched == counterfactual.matched
    )
    if (
        same_rule
        and same_verdict
        and original.weight == counterfactual.weight
        and original.contribution == counterfactual.contribution
    ):
        return "unchanged"
    if same_rule and same_verdict and original.weight != counterfactual.weight:
        return "reweighted"
    return "changed"


def _state(factor: FactorResult | None, result: EvaluationResult, key: str) -> InputState | None:
    if factor is not None:
        return factor.input_state
    return result.context_states.get(key)


def compare(counterfactual: Counterfactual) -> Comparison:
    """The comparison `PRODUCT.md` §4.5 requires, as data. Pure."""
    original = counterfactual.original
    historical_factors = {f.key: f for f in original.result.factors}
    current_factors = {f.key: f for f in counterfactual.result.factors}

    contributions = []
    for key in sorted(historical_factors.keys() | current_factors.keys()):
        before = historical_factors.get(key)
        after = current_factors.get(key)
        before_contribution = before.contribution if before is not None else 0
        after_contribution = after.contribution if after is not None else 0
        evidence_version_id = None
        for factor in (before, after):
            if factor is not None and factor.evidence_version_id is not None:
                evidence_version_id = factor.evidence_version_id
                break
        contributions.append(
            ContributionChange(
                key=key,
                evidence_version_id=evidence_version_id,
                original=before,
                counterfactual=after,
                original_state=_state(before, original.result, key),
                counterfactual_state=_state(after, counterfactual.result, key),
                original_contribution=before_contribution,
                counterfactual_contribution=after_contribution,
                contribution_delta=after_contribution - before_contribution,
                change=_classify(before, after),
            )
        )

    missing_inputs = tuple(
        MissingInput(key=factor.key, state=factor.input_state)
        for factor in sorted(counterfactual.result.factors, key=lambda f: f.key)
        if factor.input_state in _MISSING
    )

    return Comparison(
        decision_event_id=counterfactual.decision_event_id,
        decision_boundary=counterfactual.decision_boundary,
        original_label=ORIGINAL_LABEL,
        counterfactual_label=COUNTERFACTUAL_LABEL,
        historical_logic=LogicIdentity(
            logic_version=original.logic_version,
            artifact_hash=original.artifact_hash,
            evaluator_version=original.evaluator_version,
        ),
        current_logic=LogicIdentity(
            logic_version=counterfactual.current_logic_version,
            artifact_hash=counterfactual.current_artifact_hash,
            evaluator_version=counterfactual.current_evaluator_version,
        ),
        original_score=original.result.score,
        counterfactual_score=counterfactual.result.score,
        score_delta=counterfactual.result.score - original.result.score,
        original_threshold=original.result.threshold,
        counterfactual_threshold=counterfactual.result.threshold,
        original_output=original.result.output,
        counterfactual_output=counterfactual.result.output,
        output_changed=original.result.output != counterfactual.result.output,
        contributions=tuple(contributions),
        missing_inputs=missing_inputs,
        ignored_by_historical=original.result.ignored_inputs,
        ignored_by_current=counterfactual.result.ignored_inputs,
    )
