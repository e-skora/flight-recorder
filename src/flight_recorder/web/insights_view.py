"""The `/insights` read layer: one engine read at one captured cutoff, and text formatting.

A formatting layer only. `load_insights_page` captures the display cutoff once
(`ledger_maximum`, called through this module's own name), hands that cutoff
and its own connection to `insights` in exactly one call, and returns what the
engine returned. Every integer the page shows is an `Insights` field read by
attribute; every rate and difference is an engine `Fraction` formatted here.
Nothing in this module aggregates, partitions or selects rows of results.

The descriptive inputs (the signal definitions, the comparison workflow cohort)
are loaded through `fixtures.py` on each request, the same two calls the CLI
makes. Rounding is display only: one decimal, exact on the `Fraction`, a half
rounded away from zero.
"""

from dataclasses import dataclass
from fractions import Fraction

from sqlalchemy import select

from flight_recorder.analytics.insights import (
    Comparison,
    Insights,
    Rate,
    ReconstructionFailure,
    SelectionFailure,
    insights,
)
from flight_recorder.attribution.policy import ledger_maximum
from flight_recorder.fixtures import dataset_comparison_workflow_version, dataset_signals
from flight_recorder.ledger.schema import decisions

PAGE_EMPTY = "empty"
PAGE_SELECTION_FAILURE = "selection_failure"
PAGE_READY = "ready"


@dataclass(frozen=True)
class FailureLink:
    """A reconstruction failure with the stored account of its decision, for the link."""

    failure: ReconstructionFailure
    account_ref: str


@dataclass(frozen=True)
class InsightsPage:
    state: str
    cutoff: int | None = None
    result: Insights | None = None
    failure: SelectionFailure | None = None
    failure_links: tuple[FailureLink, ...] = ()


def _failure_links(conn, result: Insights) -> tuple[FailureLink, ...]:
    """Each failed decision's stored `account_ref`, read from `decisions` at the cutoff."""
    ids = [failure.decision_event_id for failure in result.reconstruction_failures]
    if not ids:
        return ()
    stored = {
        row.decision_event_id: row.account_ref
        for row in conn.execute(
            select(decisions.c.decision_event_id, decisions.c.account_ref)
            .where(decisions.c.decision_event_id.in_(ids))
            .where(decisions.c.ingest_sequence <= result.cutoff)
        )
    }
    return tuple(
        FailureLink(failure=failure, account_ref=stored[failure.decision_event_id])
        for failure in result.reconstruction_failures
    )


def load_insights_page(conn) -> InsightsPage:
    """The page at the ledger maximum captured once for this call (D-014 Q3).

    Only `SelectionFailure` is caught: it is a named data condition. Any other
    exception is a programming error and propagates.
    """
    cutoff = ledger_maximum(conn)
    if cutoff is None:
        return InsightsPage(state=PAGE_EMPTY)
    try:
        result = insights(
            conn,
            cutoff,
            signals=dataset_signals(),
            comparison_workflow_version=dataset_comparison_workflow_version(),
        )
    except SelectionFailure as failure:
        return InsightsPage(state=PAGE_SELECTION_FAILURE, cutoff=cutoff, failure=failure)
    return InsightsPage(
        state=PAGE_READY,
        cutoff=cutoff,
        result=result,
        failure_links=_failure_links(conn, result),
    )


# --- Formatting ---------------------------------------------------------------------


def _tenths(value: Fraction) -> int:
    """`value` in tenths, rounded exactly, a half away from zero."""
    scaled = abs(value) * 10
    whole, remainder = divmod(scaled.numerator, scaled.denominator)
    if 2 * remainder >= scaled.denominator:
        whole += 1
    return whole if value >= 0 else -whole


def _one_decimal(tenths: int) -> str:
    whole, tenth = divmod(abs(tenths), 10)
    return f"{whole}.{tenth}"


def percent(value: Fraction | None) -> str | None:
    """A rate as a percentage with one decimal: `Fraction(58, 149)` -> `38.9%`."""
    if value is None:
        return None
    tenths = _tenths(value * 100)
    return ("-" if tenths < 0 else "") + _one_decimal(tenths) + "%"


def points(value: Fraction | None) -> str | None:
    """A difference in percentage points, signed, one decimal; `0.0` carries no sign."""
    if value is None:
        return None
    tenths = _tenths(value)
    sign = "+" if tenths > 0 else "-" if tenths < 0 else ""
    return f"{sign}{_one_decimal(tenths)} percentage points"


def rate_line(rate: Rate) -> str:
    """`<percent> observed (<positives> of <eligible> eligible decisions; n = <eligible>)`,
    or the engine's `display_note` when the rate is not available."""
    if not rate.available:
        return rate.display_note
    return (
        f"{percent(rate.value)} observed "
        f"({rate.positives} of {rate.eligible} eligible decisions; n = {rate.eligible})"
    )


@dataclass(frozen=True)
class NoComparison:
    """The arms of a comparison with no percentage-point result, and the verb for them."""

    names: tuple[str, ...]
    verb: str


def no_comparison(
    comparison: Comparison, present_name: str, absent_name: str
) -> NoComparison | None:
    """None when the engine has a difference; otherwise the unavailable arm or arms, named."""
    if comparison.difference_points is not None:
        return None
    names = tuple(
        name
        for name, rate in ((present_name, comparison.present), (absent_name, comparison.absent))
        if not rate.available
    )
    return NoComparison(names=names, verb="have" if names[1:] else "has")


def difference_text(comparison: Comparison, present_name: str, absent_name: str) -> str:
    """`points` of the engine's difference, or
    `no comparison: <arm>[ and <arm>] has|have 0 eligible decisions`."""
    missing = no_comparison(comparison, present_name, absent_name)
    if missing is None:
        return points(comparison.difference_points)
    return f"no comparison: {' and '.join(missing.names)} {missing.verb} 0 eligible decisions"
