"""The closed rule grammar `evaluator-v1` understands.

A schema-v1 logic artifact stores each factor's rule as a prose string. This
module parses exactly the four shapes the registered artifacts use and refuses
everything else, so a rule the evaluator cannot interpret deterministically is
an explicit failure rather than a silent non-match (INV-05, INV-09):

- `<key> between <a> and <b> inclusive` -- integer value, `a <= value <= b`
- `<key> equals '<literal>'` -- string value, exact case-sensitive equality
- `<key> at least <n>` -- integer value, `value >= n`
- `<key> observed within <n> days before the decision boundary` -- temporal
  window, see below

The grammar is deliberately narrow: each pattern must match the rule text in
full, matching is case-sensitive, bounds are integers, and literals are
single-quoted. An extra word, a capitalized keyword, a float bound, a missing
quote, an unknown verb, or so much as a leading space or trailing newline
raises `UnsupportedRule`. There is no fallback and no fuzzy matching.

**Temporal semantics (introduced by `evaluator-v1`).** `PRODUCT.md` does not
define this precision; the rule below is this evaluator's semantics. The linked
evidence version's `observed_at` is a calendar date `o`, interpreted as the
instant `o` at `00:00:00Z`. `T(d)` is the decision boundary. The rule matches
when `o <= T(d)` and the elapsed time `T(d) - o` is at most `n x 24 hours`.
This is elapsed time, not calendar-day counting: against a non-midnight
boundary, a date exactly `n` calendar days earlier falls outside the window.

Parsing is pure and touches neither the database nor the clock.
"""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

__all__ = [
    "AtLeastRule",
    "BetweenRule",
    "EqualsRule",
    "ObservedWithinRule",
    "Rule",
    "RuleError",
    "RuleKeyMismatch",
    "RuleTypeError",
    "UnsupportedBoundary",
    "UnsupportedRule",
    "parse_boundary",
    "parse_rule",
    "require_aware_boundary",
]


class RuleError(Exception):
    """Base class for every explicit rule failure."""


class UnsupportedRule(RuleError):
    """The rule text is not one of the four supported shapes."""

    def __init__(self, key: str, text: str):
        super().__init__(
            f"factor {key!r}: rule {text!r} is not a shape evaluator-v1 supports; "
            "exact replay cannot interpret it"
        )
        self.key = key
        self.text = text


class RuleKeyMismatch(RuleError):
    """The key inside the rule text is not the factor's key."""

    def __init__(self, key: str, rule_key: str, text: str):
        super().__init__(
            f"factor {key!r}: rule {text!r} names input {rule_key!r}, not the factor's key"
        )
        self.key = key
        self.rule_key = rule_key
        self.text = text


class RuleTypeError(RuleError):
    """The preserved value or its metadata does not fit the rule's shape."""

    def __init__(self, key: str, detail: str):
        super().__init__(f"factor {key!r}: {detail}")
        self.key = key
        self.detail = detail


class UnsupportedBoundary(RuleError):
    """A decision boundary that is not an explicit UTC instant."""

    def __init__(self, text: str, detail: str):
        super().__init__(f"decision_boundary {text!r}: {detail}")
        self.text = text
        self.detail = detail


_KEY = r"[A-Za-z_][A-Za-z0-9_]*"

_BETWEEN = re.compile(rf"^(?P<key>{_KEY}) between (?P<low>-?\d+) and (?P<high>-?\d+) inclusive$")
_EQUALS = re.compile(rf"^(?P<key>{_KEY}) equals '(?P<literal>[^']*)'$")
_AT_LEAST = re.compile(rf"^(?P<key>{_KEY}) at least (?P<minimum>-?\d+)$")
_OBSERVED_WITHIN = re.compile(
    rf"^(?P<key>{_KEY}) observed within (?P<days>\d+) days before the decision boundary$"
)


def _require_int(key: str, value, shape: str) -> int:
    """The preserved value as an integer, refusing `bool` and every other type."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuleTypeError(
            key,
            f"{shape} needs an integer value; the preserved value is "
            f"{type(value).__name__} {value!r}",
        )
    return value


def _reject_bool(key: str, value) -> None:
    if isinstance(value, bool):
        raise RuleTypeError(key, f"a boolean value ({value!r}) fits no evaluator-v1 rule shape")


@dataclass(frozen=True)
class BetweenRule:
    """`<key> between <low> and <high> inclusive`."""

    key: str
    low: int
    high: int

    def matches(self, value, *, observed_at: date | None, boundary: datetime) -> bool:
        number = _require_int(self.key, value, "an inclusive range rule")
        return self.low <= number <= self.high


@dataclass(frozen=True)
class EqualsRule:
    """`<key> equals '<literal>'`, exact and case-sensitive."""

    key: str
    literal: str

    def matches(self, value, *, observed_at: date | None, boundary: datetime) -> bool:
        _reject_bool(self.key, value)
        if not isinstance(value, str):
            raise RuleTypeError(
                self.key,
                "an equality rule needs a string value; the preserved value is "
                f"{type(value).__name__} {value!r}",
            )
        return value == self.literal


@dataclass(frozen=True)
class AtLeastRule:
    """`<key> at least <minimum>`."""

    key: str
    minimum: int

    def matches(self, value, *, observed_at: date | None, boundary: datetime) -> bool:
        number = _require_int(self.key, value, "an at-least rule")
        return number >= self.minimum


@dataclass(frozen=True)
class ObservedWithinRule:
    """`<key> observed within <days> days before the decision boundary`.

    The preserved value itself is not compared; the linked evidence version's
    `observed_at` is. See the module docstring for the elapsed-time semantics.
    """

    key: str
    days: int

    def matches(self, value, *, observed_at: date | None, boundary: datetime) -> bool:
        _reject_bool(self.key, value)
        if observed_at is None:
            raise RuleTypeError(
                self.key,
                "an observed-within rule needs the linked evidence version's observed_at, "
                "which is not recorded for this input",
            )
        if not isinstance(observed_at, date) or isinstance(observed_at, datetime):
            raise RuleTypeError(
                self.key,
                f"observed_at must be a calendar date; got {type(observed_at).__name__} "
                f"{observed_at!r}",
            )
        instant = datetime(observed_at.year, observed_at.month, observed_at.day, tzinfo=UTC)
        boundary = require_aware_boundary(boundary)
        if instant > boundary:
            return False
        return boundary - instant <= timedelta(days=self.days)


Rule = BetweenRule | EqualsRule | AtLeastRule | ObservedWithinRule


def parse_rule(key: str, text: str) -> Rule:
    """Parse one factor's rule text, or raise.

    `key` is the factor's own key; a rule naming a different input is a
    `RuleKeyMismatch`, not a silent reinterpretation.
    """
    for pattern, build in (
        (_BETWEEN, lambda m: BetweenRule(m["key"], int(m["low"]), int(m["high"]))),
        (_EQUALS, lambda m: EqualsRule(m["key"], m["literal"])),
        (_AT_LEAST, lambda m: AtLeastRule(m["key"], int(m["minimum"]))),
        (_OBSERVED_WITHIN, lambda m: ObservedWithinRule(m["key"], int(m["days"]))),
    ):
        # `fullmatch`, not `match`: `$` also matches immediately before a
        # trailing newline, so anchors alone would admit `"... inclusive\n"`
        # as a valid rule. A closed grammar accepts the exact string only.
        match = pattern.fullmatch(text)
        if match is None:
            continue
        if match["key"] != key:
            raise RuleKeyMismatch(key, match["key"], text)
        return build(match)
    raise UnsupportedRule(key, text)


def require_aware_boundary(boundary: datetime) -> datetime:
    """`T(d)` as a UTC-aware instant, refusing a naive one (INV-02, INV-09).

    Public because every evaluation path must apply it, not only the temporal
    rule: a naive boundary is never assumed to be UTC, whatever the factors do.
    """
    if boundary.tzinfo is None or boundary.tzinfo.utcoffset(boundary) is None:
        raise UnsupportedBoundary(
            boundary.isoformat(),
            "a decision boundary must carry an explicit UTC offset; a naive instant is "
            "never assumed to be UTC",
        )
    return boundary.astimezone(UTC)


def parse_boundary(text: str) -> datetime:
    """`T(d)` from the stored D-010 timestamp text, as a UTC-aware instant.

    Text without an explicit UTC designator is an explicit error (INV-02): the
    evaluator never guesses a timezone for a decision boundary.
    """
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError) as exc:
        raise UnsupportedBoundary(str(text), f"is not an ISO-8601 instant ({exc})") from exc
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise UnsupportedBoundary(
            text,
            "carries no UTC designator; a naive instant is never assumed to be UTC",
        )
    return parsed.astimezone(UTC)
