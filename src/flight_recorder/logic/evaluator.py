"""`evaluator-v1`: deterministic evaluation of a logic artifact over `H(d)`.

`R(L, H)` from `PRODUCT.md` §5. The evaluator is pure: it is handed the
validated artifact, the preserved historical context, and the decision
boundary, and it computes a result. It never reads the database, never resolves
"the latest" anything, and never substitutes a present-day value for a missing
one (INV-02, INV-03, INV-09).

`EVALUATOR_VERSION` is this evaluator's identity. Together with the artifact's
canonical content hash it is what makes replay exact (INV-05); the `v3.2` label
is metadata, not identity.

Input states, per preserved entry of `H(d)` (INV-03):

- `consumed` -- available and referenced by a factor of this artifact, whether
  or not the rule matched. Consumption and matching are distinct: a consumed
  input that fails its rule contributes 0 and stays consumed.
- `ignored` -- available and referenced by no factor of this artifact.
- `unavailable` -- explicitly recorded as unavailable at `T(d)`.
- `absent` -- a factor's key has no entry in `H(d)` at all. This is a state of
  the factor, not of a preserved input, so it appears in `factors` and never in
  `context_states`.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from types import MappingProxyType

from flight_recorder.collector.schema import LogicArtifact, ScalarValue
from flight_recorder.logic.rules import Rule, parse_rule, require_aware_boundary

__all__ = [
    "EVALUATOR_VERSION",
    "ContextInput",
    "DuplicateContextKey",
    "EvaluationError",
    "EvaluationResult",
    "FactorResult",
    "InputState",
    "UnsupportedMissingValueBehavior",
    "evaluate",
]

#: This evaluator's code identity (INV-05). Changing evaluation behavior
#: requires a new value here, not an edit to an existing one.
EVALUATOR_VERSION = "evaluator-v1"

#: The only `missing_value_behavior` `evaluator-v1` implements.
SUPPORTED_MISSING_VALUE_BEHAVIOR = "no_match_contributes_zero"


class InputState(StrEnum):
    CONSUMED = "consumed"
    IGNORED = "ignored"
    UNAVAILABLE = "unavailable"
    ABSENT = "absent"


class EvaluationError(Exception):
    """Base class for explicit evaluator failures."""


class UnsupportedMissingValueBehavior(EvaluationError):
    """The artifact declares a missing-value behavior this evaluator cannot honor.

    The strict `LogicArtifact` model admits any non-empty string, so the
    evaluator refuses rather than guessing what an unknown behavior means.
    """

    def __init__(self, behavior: str):
        super().__init__(
            f"missing_value_behavior {behavior!r} is not implemented by "
            f"{EVALUATOR_VERSION}; the only supported behavior is "
            f"{SUPPORTED_MISSING_VALUE_BEHAVIOR!r}"
        )
        self.behavior = behavior


class DuplicateContextKey(EvaluationError):
    """`H(d)` presented the same input key more than once."""

    def __init__(self, key: str):
        super().__init__(f"historical context has more than one entry for input_key {key!r}")
        self.key = key


@dataclass(frozen=True)
class ContextInput:
    """One preserved entry of `H(d)`.

    `observed_at` is the *linked evidence version's* observation date, not
    anything taken from the decision payload; it is `None` when the evidence
    type carries no observation date, or when the input was unavailable.
    """

    key: str
    availability: str  # "available" | "unavailable"
    value: ScalarValue = None
    evidence_version_id: str | None = None
    observed_at: date | None = None

    @property
    def is_available(self) -> bool:
        return self.availability == "available"


@dataclass(frozen=True)
class FactorResult:
    """One artifact factor, evaluated."""

    key: str
    rule: str
    weight: int
    input_state: InputState
    matched: bool
    contribution: int
    evidence_version_id: str | None


@dataclass(frozen=True)
class EvaluationResult:
    """`R(L, H)`: the score, the output, and how every factor and input got there."""

    score: int
    threshold: int
    output: str
    factors: tuple[FactorResult, ...]
    #: Available preserved inputs no factor of this artifact references.
    ignored_inputs: tuple[str, ...]
    #: Every preserved input of `H(d)`, mapped to its state, so an unreferenced
    #: unavailable entry stays visible rather than disappearing from the result.
    context_states: Mapping[str, InputState]

    @property
    def consumed_triples(self) -> frozenset[tuple[str, str | None, int]]:
        """`(input_key, evidence_version_id, contribution)` for the consumed factors.

        The comparison key against `decision_consumed_inputs`: every factor
        whose input was consumed, including a tested non-match contributing 0
        and a zero-weight match. `unavailable` and `absent` factors are not
        consumed inputs and are excluded.
        """
        return frozenset(
            (factor.key, factor.evidence_version_id, factor.contribution)
            for factor in self.factors
            if factor.input_state is InputState.CONSUMED
        )


def _index(context: Sequence[ContextInput]) -> dict[str, ContextInput]:
    indexed: dict[str, ContextInput] = {}
    for entry in context:
        if entry.key in indexed:
            raise DuplicateContextKey(entry.key)
        indexed[entry.key] = entry
    return indexed


def evaluate(
    artifact: LogicArtifact,
    context: Sequence[ContextInput],
    boundary: datetime,
) -> EvaluationResult:
    """Evaluate `artifact` over the preserved context `context` at `boundary`.

    The boundary is validated at entry and every rule is parsed before any
    factor is evaluated, so a naive boundary and an unsupported rule both fail
    even when the input they concern is unavailable or absent.
    """
    # INV-02: on every path, not only the one where a temporal rule runs.
    boundary = require_aware_boundary(boundary)

    if artifact.missing_value_behavior != SUPPORTED_MISSING_VALUE_BEHAVIOR:
        raise UnsupportedMissingValueBehavior(artifact.missing_value_behavior)

    preserved = _index(context)
    rules: list[Rule] = [parse_rule(factor.key, factor.rule) for factor in artifact.factors]

    results: list[FactorResult] = []
    for factor, rule in zip(artifact.factors, rules, strict=True):
        entry = preserved.get(factor.key)
        if entry is None:
            state, matched, evidence_version_id = InputState.ABSENT, False, None
        elif not entry.is_available:
            state, matched, evidence_version_id = InputState.UNAVAILABLE, False, None
        else:
            state = InputState.CONSUMED
            matched = rule.matches(entry.value, observed_at=entry.observed_at, boundary=boundary)
            evidence_version_id = entry.evidence_version_id
        results.append(
            FactorResult(
                key=factor.key,
                rule=factor.rule,
                weight=factor.weight,
                input_state=state,
                matched=matched,
                contribution=factor.weight if matched else 0,
                evidence_version_id=evidence_version_id,
            )
        )

    referenced = {factor.key for factor in artifact.factors}
    score = sum(result.contribution for result in results)
    mapping = artifact.output_mapping
    output = (
        mapping.at_or_above_threshold if score >= artifact.threshold else mapping.below_threshold
    )

    context_states = {
        entry.key: (
            InputState.UNAVAILABLE
            if not entry.is_available
            else (InputState.CONSUMED if entry.key in referenced else InputState.IGNORED)
        )
        for entry in context
    }

    return EvaluationResult(
        score=score,
        threshold=artifact.threshold,
        output=output,
        factors=tuple(results),
        ignored_inputs=tuple(
            key for key, state in context_states.items() if state is InputState.IGNORED
        ),
        context_states=MappingProxyType(context_states),
    )
