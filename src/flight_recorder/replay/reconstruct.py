"""Exact reconstruction of a recorded decision: `R(Lh(d), H(d))`.

This is the original half of `PRODUCT.md` §4.5. It answers one question --
*does the preserved historical logic, re-run over the preserved historical
context, still produce the decision that was recorded?* -- and answers it from
the projection tables alone. The counterfactual half, `R(Lc, H(d))`, is a
separate computation with separate labels and arrives in Phase 3 (INV-06).

Verification comes before evaluation (INV-05, D-005). `reconstruct` performs
these steps in order and stops at the first failure:

1. load the `decisions` row (`DecisionNotFound`);
2. load the `logic_artifacts` row by the decision's `artifact_hash`
   (`ArtifactMissing`);
3. decode `artifact_json` (`IntegrityFailure`, field `artifact_json`);
4. recompute the canonical content hash and require it to equal both the row's
   and the decision's `artifact_hash` (field `artifact_hash`);
5. validate the stored text through the strict `LogicArtifact` model (field
   `artifact_schema`);
6. compare the validated content's identity fields with the row's columns;
7. compare the decision's declared logic identity with the verified artifact;
8. require the verified `evaluator_version` to equal this runtime's
   `EVALUATOR_VERSION` (field `evaluator_version`);
9. load `H(d)` for this decision, resolving each linked evidence version by the
   immutable id the decision preserved and requiring its `available_at` not to
   be after the decision boundary (field `available_at`) -- for every preserved
   reference, consumed or ignored, before anything is evaluated (INV-02);
10. parse every rule, then evaluate;
11. compare the result with what was recorded (`ReconstructionMismatch`).

Steps 2-8 are the pure helper `verify_artifact`, which the integrity tests call
directly; `reconstruct` calls that same helper rather than a parallel copy.

Reads only. Reconstruction writes nothing anywhere, and touches neither
`events` nor `accounts`: evidence is resolved by the preserved
`evidence_version_id`, never by account or recency (INV-01, INV-02).

Step 9 verifies the sealed context; it never rebuilds it. Membership in `H(d)`
is what the decision preserved, the supersession link is never followed, and no
timestamp decides which version to read. The availability check only refuses a
preserved reference that the collector would have refused at ingest, so a row
that reached `evidence_versions` without crossing the collector cannot smuggle
later evidence into a reconstruction.
"""

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select

from flight_recorder.collector.canonical import canonical_hash
from flight_recorder.collector.schema import LogicArtifact
from flight_recorder.ledger.schema import (
    decision_consumed_inputs,
    decision_context,
    decisions,
    evidence_versions,
    logic_artifacts,
)
from flight_recorder.logic import evaluator as evaluator_module
from flight_recorder.logic.evaluator import ContextInput, EvaluationResult, evaluate
from flight_recorder.logic.rules import parse_boundary

__all__ = [
    "ArtifactMissing",
    "ArtifactRow",
    "ConsumedInputRow",
    "DecisionNotFound",
    "DecisionRow",
    "EvidenceVersionRow",
    "IntegrityFailure",
    "Reconstruction",
    "ReconstructionError",
    "ReconstructionMismatch",
    "VerifiedArtifact",
    "compare_with_recorded",
    "load_artifact_row",
    "load_consumed_inputs",
    "load_context",
    "load_decision_row",
    "load_evidence_version",
    "reconstruct",
    "verify_artifact",
]


# --- Failures ---------------------------------------------------------------


class ReconstructionError(Exception):
    """Base class for every explicit reconstruction failure.

    There is no best-effort result: a reconstruction either reproduces the
    recorded decision exactly or raises (INV-05, INV-09).
    """


class DecisionNotFound(ReconstructionError):
    def __init__(self, decision_event_id: str):
        super().__init__(f"no recorded decision with decision_event_id {decision_event_id!r}")
        self.decision_event_id = decision_event_id


class ArtifactMissing(ReconstructionError):
    """The decision's logic artifact is not stored, so exact replay is impossible."""

    def __init__(self, artifact_hash: str):
        super().__init__(
            f"logic artifact {artifact_hash!r} is not registered; exact replay of this "
            "decision cannot be verified"
        )
        self.artifact_hash = artifact_hash


class IntegrityFailure(ReconstructionError):
    """A verification step failed, named by the field that disagrees.

    `stored` is the value the ledger holds. `recomputed` is the value derived
    from the verified content: the hash recomputed from `artifact_json` for a
    content-hash failure, the artifact's own field for an identity comparison,
    and this runtime's `EVALUATOR_VERSION` for the evaluator check.
    """

    def __init__(
        self,
        field: str,
        detail: str,
        *,
        stored: Any = None,
        recomputed: Any = None,
    ):
        super().__init__(f"integrity failure on {field}: {detail}")
        self.field = field
        self.detail = detail
        self.stored = stored
        self.recomputed = recomputed


class ReconstructionMismatch(ReconstructionError):
    """The reconstruction differs from the recorded decision.

    Raised instead of returning a result, so the function can never present a
    divergent answer as exact.
    """

    def __init__(self, field: str, recorded: Any, reconstructed: Any):
        super().__init__(
            f"reconstruction differs from the recorded decision on {field}: "
            f"recorded {recorded!r}, reconstructed {reconstructed!r}"
        )
        self.field = field
        self.recorded = recorded
        self.reconstructed = reconstructed


# --- Row views --------------------------------------------------------------
#
# Plain frozen views of the columns each step verifies, so the pure helpers can
# be exercised without a database while `reconstruct` feeds them real rows.


@dataclass(frozen=True)
class ArtifactRow:
    artifact_hash: str
    artifact_id: str
    artifact_schema_version: str
    logic_version: str
    decision_class: str
    evaluator_version: str
    artifact_json: str


@dataclass(frozen=True)
class DecisionRow:
    decision_event_id: str
    account_ref: str
    decision_class: str
    decision_boundary: str
    artifact_hash: str
    logic_version: str
    evaluator_version: str
    score: int
    threshold: int
    output: str


@dataclass(frozen=True)
class ConsumedInputRow:
    input_key: str
    evidence_version_id: str
    contribution: int


@dataclass(frozen=True)
class VerifiedArtifact:
    """The artifact whose identity has been verified, plus what was checked."""

    artifact: LogicArtifact
    stored_artifact_hash: str
    recomputed_artifact_hash: str
    #: The runtime evaluator identity the artifact was required to match.
    runtime_evaluator_version: str


@dataclass(frozen=True)
class Reconstruction:
    """An exact reconstruction of one recorded decision."""

    decision_event_id: str
    artifact_hash: str
    logic_version: str
    evaluator_version: str
    decision_boundary: datetime
    result: EvaluationResult
    #: What verification established, before any evaluation ran.
    stored_artifact_hash: str
    recomputed_artifact_hash: str
    runtime_evaluator_version: str


# --- Verification (steps 2-8) ----------------------------------------------


_IDENTITY_FIELDS = (
    "artifact_id",
    "artifact_schema_version",
    "logic_version",
    "decision_class",
    "evaluator_version",
)

#: The decision's own labels. `decisions` has no `artifact_id` column: the
#: collector validated the submitted artifact id against the registered row at
#: ingest, and reconstruction does not read the raw event to recover it.
_DECISION_IDENTITY_FIELDS = ("logic_version", "decision_class", "evaluator_version")


def _decode_artifact_json(text: str) -> dict:
    def _reject(constant: str):
        raise ValueError(f"{constant} is not valid JSON for a logic artifact")

    try:
        decoded = json.loads(text, parse_constant=_reject)
    except (TypeError, ValueError) as exc:
        raise IntegrityFailure(
            "artifact_json",
            f"the stored artifact text is not decodable JSON ({exc})",
            stored=text,
        ) from exc
    if not isinstance(decoded, dict):
        raise IntegrityFailure(
            "artifact_json",
            f"the stored artifact text decodes to {type(decoded).__name__}, not an object",
            stored=text,
        )
    return decoded


def verify_artifact(
    artifact_row: ArtifactRow | None,
    decision_row: DecisionRow,
    *,
    runtime_evaluator_version: str | None = None,
) -> VerifiedArtifact:
    """Steps 2-8: prove the stored artifact is the one the decision used.

    Nothing is evaluated here. Every failure is explicit and names the field
    that disagrees, so a mismatched artifact can never become a best-effort
    answer presented as exact (INV-05, INV-09).
    """
    if runtime_evaluator_version is None:
        # Resolved at call time so the runtime identity can be substituted in a
        # test without reaching past this module's import.
        runtime_evaluator_version = evaluator_module.EVALUATOR_VERSION

    if artifact_row is None:
        raise ArtifactMissing(decision_row.artifact_hash)

    content = _decode_artifact_json(artifact_row.artifact_json)

    try:
        recomputed = canonical_hash(content)
    except ValueError as exc:  # pragma: no cover - guarded by _decode_artifact_json
        raise IntegrityFailure(
            "artifact_json",
            f"the stored artifact text cannot be canonicalized ({exc})",
            stored=artifact_row.artifact_json,
        ) from exc

    if recomputed != artifact_row.artifact_hash:
        raise IntegrityFailure(
            "artifact_hash",
            f"the stored content hashes to {recomputed}, not the registered "
            f"{artifact_row.artifact_hash}",
            stored=artifact_row.artifact_hash,
            recomputed=recomputed,
        )
    if recomputed != decision_row.artifact_hash:
        raise IntegrityFailure(
            "artifact_hash",
            f"the decision names artifact {decision_row.artifact_hash}, but the verified "
            f"content hashes to {recomputed}",
            stored=decision_row.artifact_hash,
            recomputed=recomputed,
        )

    try:
        artifact = LogicArtifact.model_validate_json(artifact_row.artifact_json, strict=True)
    except ValidationError as exc:
        raise IntegrityFailure(
            "artifact_schema",
            f"the stored artifact does not satisfy the schema-v1 logic artifact model ({exc})",
            stored=artifact_row.artifact_json,
        ) from exc

    for field in _IDENTITY_FIELDS:
        row_value = getattr(artifact_row, field)
        content_value = getattr(artifact, field)
        if row_value != content_value:
            raise IntegrityFailure(
                field,
                f"the logic_artifacts row says {row_value!r}, but the verified content says "
                f"{content_value!r}",
                stored=row_value,
                recomputed=content_value,
            )

    for field in _DECISION_IDENTITY_FIELDS:
        decision_value = getattr(decision_row, field)
        content_value = getattr(artifact, field)
        if decision_value != content_value:
            raise IntegrityFailure(
                field,
                f"the decision says {decision_value!r}, but the verified artifact says "
                f"{content_value!r}",
                stored=decision_value,
                recomputed=content_value,
            )

    if artifact.evaluator_version != runtime_evaluator_version:
        raise IntegrityFailure(
            "evaluator_version",
            f"the decision was produced by {artifact.evaluator_version!r}; this runtime is "
            f"{runtime_evaluator_version!r}, which cannot claim to replay it exactly",
            stored=artifact.evaluator_version,
            recomputed=runtime_evaluator_version,
        )

    return VerifiedArtifact(
        artifact=artifact,
        stored_artifact_hash=artifact_row.artifact_hash,
        recomputed_artifact_hash=recomputed,
        runtime_evaluator_version=runtime_evaluator_version,
    )


# --- Comparison (step 11) ---------------------------------------------------


def compare_with_recorded(
    result: EvaluationResult,
    decision_row: DecisionRow,
    consumed_rows: tuple[ConsumedInputRow, ...],
) -> None:
    """Step 11: require the reconstruction to equal the recorded decision.

    The consumed comparison is a set equality over
    `(input_key, evidence_version_id, contribution)` triples. Every factor
    whose input was consumed appears, including a tested non-match contributing
    0 and a zero-weight match; `unavailable` and `absent` factors do not.
    """
    for field, recorded in (
        ("score", decision_row.score),
        ("threshold", decision_row.threshold),
        ("output", decision_row.output),
    ):
        reconstructed = getattr(result, field)
        if recorded != reconstructed:
            raise ReconstructionMismatch(field, recorded, reconstructed)

    recorded_triples = frozenset(
        (row.input_key, row.evidence_version_id, row.contribution) for row in consumed_rows
    )
    if recorded_triples != result.consumed_triples:
        raise ReconstructionMismatch(
            "consumed_inputs", sorted(recorded_triples), sorted(result.consumed_triples)
        )


# --- Loading ----------------------------------------------------------------


def load_decision_row(conn, decision_event_id: str) -> DecisionRow:
    row = conn.execute(
        select(decisions).where(decisions.c.decision_event_id == decision_event_id)
    ).first()
    if row is None:
        raise DecisionNotFound(decision_event_id)
    return DecisionRow(
        decision_event_id=row.decision_event_id,
        account_ref=row.account_ref,
        decision_class=row.decision_class,
        decision_boundary=row.decision_boundary,
        artifact_hash=row.artifact_hash,
        logic_version=row.logic_version,
        evaluator_version=row.evaluator_version,
        score=row.score,
        threshold=row.threshold,
        output=row.output,
    )


def load_artifact_row(conn, artifact_hash: str) -> ArtifactRow | None:
    row = conn.execute(
        select(logic_artifacts).where(logic_artifacts.c.artifact_hash == artifact_hash)
    ).first()
    if row is None:
        return None
    return ArtifactRow(
        artifact_hash=row.artifact_hash,
        artifact_id=row.artifact_id,
        artifact_schema_version=row.artifact_schema_version,
        logic_version=row.logic_version,
        decision_class=row.decision_class,
        evaluator_version=row.evaluator_version,
        artifact_json=row.artifact_json,
    )


@dataclass(frozen=True)
class EvidenceVersionRow:
    """The two columns of one evidence version that reconstruction reads."""

    evidence_version_id: str
    observed_at: date | None
    #: Stored D-010 text, `YYYY-MM-DDTHH:MM:SS.ffffffZ`.
    available_at: str


def load_evidence_version(conn, evidence_version_id: str) -> EvidenceVersionRow:
    """One evidence version's `observed_at` and `available_at`, by primary key.

    This is the only read of `evidence_versions` in the whole reconstruction,
    and it is by the immutable id the decision preserved. The supersession link
    is never followed, no `account_ref` or `evidence_type` filter is applied,
    and no newer version is ever consulted (INV-01, INV-02).
    """
    row = conn.execute(
        select(evidence_versions.c.observed_at, evidence_versions.c.available_at).where(
            evidence_versions.c.evidence_version_id == evidence_version_id
        )
    ).first()
    if row is None:
        raise IntegrityFailure(
            "evidence_version_id",
            f"the preserved context references evidence version {evidence_version_id!r}, "
            "which is not stored",
            stored=evidence_version_id,
        )
    return EvidenceVersionRow(
        evidence_version_id=evidence_version_id,
        observed_at=date.fromisoformat(row.observed_at) if row.observed_at is not None else None,
        available_at=row.available_at,
    )


def load_context(conn, decision_event_id: str, decision_boundary: str) -> tuple[ContextInput, ...]:
    """`H(d)`: every preserved input of one decision, in a stable key order.

    Every entry that carries an `evidence_version_id` is resolved by that id
    and its stored `available_at` is required not to be after
    `decision_boundary` (INV-02: `available_at(e) > T(d) => e not in H(d)`).
    The check runs here, at the context read, for every preserved reference --
    a historically available input no factor consumes is checked exactly like a
    consumed one -- and before any evaluation. A later reference is an
    `IntegrityFailure` on field `available_at`, with `stored` the row's
    `available_at` and `recomputed` the boundary text. Equality is admitted.

    Precondition: `decision_boundary` is the decision's stored boundary text
    from the `decisions` row (or a value produced by `format_utc`), and both it
    and every `available_at` are in the collector's fixed-width normalized
    format `YYYY-MM-DDTHH:MM:SS.ffffffZ`, on which lexical order is
    chronological order. The comparison is the same string comparison the
    collector makes at ingest (`_validate_decision`); an arbitrary ISO string
    is not a valid argument.

    This verifies the sealed context; it does not rebuild it. Membership stays
    what the decision preserved and the supersession link is never followed.
    """
    rows = conn.execute(
        select(decision_context)
        .where(decision_context.c.decision_event_id == decision_event_id)
        .order_by(decision_context.c.input_key)
    ).all()
    context: list[ContextInput] = []
    for row in rows:
        evidence = (
            load_evidence_version(conn, row.evidence_version_id)
            if row.evidence_version_id is not None
            else None
        )
        if evidence is not None and evidence.available_at > decision_boundary:
            raise IntegrityFailure(
                "available_at",
                f"the preserved context of {decision_event_id!r} references evidence version "
                f"{evidence.evidence_version_id!r} for {row.input_key!r}, available at "
                f"{evidence.available_at}, after the decision boundary {decision_boundary}",
                stored=evidence.available_at,
                recomputed=decision_boundary,
            )
        context.append(
            ContextInput(
                key=row.input_key,
                availability=row.availability,
                # The same canonical JSON codec the projection wrote with, so
                # `184`, `"184"` and `true` stay distinguishable.
                value=json.loads(row.value_text) if row.value_text is not None else None,
                evidence_version_id=row.evidence_version_id,
                observed_at=evidence.observed_at if evidence is not None else None,
            )
        )
    return tuple(context)


def load_consumed_inputs(conn, decision_event_id: str) -> tuple[ConsumedInputRow, ...]:
    rows = conn.execute(
        select(decision_consumed_inputs)
        .where(decision_consumed_inputs.c.decision_event_id == decision_event_id)
        .order_by(decision_consumed_inputs.c.input_key)
    ).all()
    return tuple(
        ConsumedInputRow(
            input_key=row.input_key,
            evidence_version_id=row.evidence_version_id,
            contribution=row.contribution,
        )
        for row in rows
    )


# --- The reconstruction -----------------------------------------------------


def reconstruct(conn, decision_event_id: str) -> Reconstruction:
    """Reproduce one recorded decision from the projection tables alone.

    Raises rather than returning anything that differs from the record. Writes
    nothing; reads no `events` and no `accounts` row.
    """
    decision_row = load_decision_row(conn, decision_event_id)
    artifact_row = load_artifact_row(conn, decision_row.artifact_hash)
    verified = verify_artifact(artifact_row, decision_row)

    boundary = parse_boundary(decision_row.decision_boundary)
    context = load_context(conn, decision_event_id, decision_row.decision_boundary)
    result = evaluate(verified.artifact, context, boundary)

    compare_with_recorded(result, decision_row, load_consumed_inputs(conn, decision_event_id))

    return Reconstruction(
        decision_event_id=decision_row.decision_event_id,
        artifact_hash=verified.recomputed_artifact_hash,
        logic_version=verified.artifact.logic_version,
        evaluator_version=verified.artifact.evaluator_version,
        decision_boundary=boundary,
        result=result,
        stored_artifact_hash=verified.stored_artifact_hash,
        recomputed_artifact_hash=verified.recomputed_artifact_hash,
        runtime_evaluator_version=verified.runtime_evaluator_version,
    )
