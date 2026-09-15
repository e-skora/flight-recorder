"""The seeded synthetic dataset generator (D-008, D-014 Q4, D-015).

`generate(config, artifacts=...)` returns the complete seed schedule as plain
data. It touches no database and no network; `random.Random(config.seed)` is its
only source of variation, and every collection it iterates has a fixed order.
Every account, name, value and outcome is synthetic: names are built from
invented fragments, domains end in `.example`, and no person is named.

**What it emits.** Prospect accounts `ds-0001` ... with `account.discovered`,
`evidence.recorded` (the seven typed evidence keys only), `decision.recorded`
evaluated by `evaluator-v1` over the context it built (so every decision
reconstructs exactly), `persona.selected` and `action.recorded` for prioritized
decisions, and schema-v2 `outcome.evaluated` observations. It never emits the
canonical account or the `_system` principal and never registers an artifact;
decisions reference the canonical artifacts by their content hash.

**Artifact selection.** Activation instants are read from the artifacts. A
boundary inside the earlier artifact's activation window uses it; a boundary at
or after the later artifact's activation uses that one. One generated boundary
is exactly the later activation instant and one is a microsecond before it.

**Outcomes** (D-015). An observation opens at its action's `occurred_at`
(action-backed) or, for a decision with no action recorded, at the preserved
decision boundary. It closes after its period; it is `closed` and stamped at
close when that is at or before the horizon `H`, else `open` and stamped at a
configured age, never after `H`. A decision-referenced observation names its
decision; a reference-free one names nothing and is placed only on accounts
holding no non-failed action at all, so no eligible action exists within any
lookback. A missing observation stays missing. `opportunity` is assigned per
workflow cohort and integration-pressure group from the configured rates. The
generator computes no attribution and checks no planted effect; the effects are
proven through ingestion and the analytics engine.

**Config parameters** (`fixtures/dataset/config.json`):

- `seed`, `account_count`, `horizon` (`H`), `attribution_instant` (`A >= H`),
  `comparison_workflow_version` (the cohort compared with the canonical
  decision's workflow version; it must differ from it).
- `decisions`: `second_decision_share` (accounts with one decision under each
  artifact), `current_logic_share` (single-decision accounts decided under the
  later artifact), `compared_workflow_share` (decisions on the canonical
  decision's workflow version), `prioritize_intent_share` (accounts with recent
  funding whose first decision falls under the earlier artifact and whose
  firmographic options are searched for the at-or-above-threshold output; the
  others are searched for the below-threshold output), `boundary_margin_hours`
  (the latest boundary is this far before `H`), `explanation_share`,
  `explanations`.
- `evidence`: option lists `employee_count` and
  `open_platform_engineering_roles` (integer `range`s), `industry` and
  `headquarters_country` (`values`), each option with a `share`;
  `open_platform_engineering_roles_share` and `head_of_platform_share` (key
  present); `funding_mix` (`recent`, `stale`, `absent`, `unavailable`) with
  `funding_rounds`, `funding_recent_days` and `funding_stale_days` (days before
  the first boundary); `pressure_mix` (`HIGH`, `MEDIUM`, `LOW`, `absent`,
  `unavailable`) with `pressure_basis`; `website_intent_unavailable_share`.
- `actions`: `status_mix` over prioritized decisions (`none`, `failed`,
  `sent`, `completed`; at least two failed when there are enough), `plays`,
  `personas` (job titles), `cost_cents`.
- `outcomes`: `action_observation_mix` (`own_claim`, `no_claim`, `none`),
  `failed_action_observation_mix` (`claim`, `no_claim`),
  `no_action_observation_mix` (`decision_reference`, `reference_free`,
  `none`), `period_mix` (days), `closed_unknown_share`, `open_age_days`,
  `open_unknown_share`, `reply_share`, `meeting_share`,
  `recorded_delay_minutes`, and `opportunity_rates` per workflow version with a
  `HIGH` rate and an `other` rate.
- `stage_2`: `correction` (`account_index`, `outcome_n`, `period_days`,
  `opportunity`) names the stage-1 open observation that a closed version
  replaces; `new_observation` (`account_index`, `decision_n`, `period_days`,
  `opportunity`) names a decision without a stage-1 observation.

Decks: a share over a set becomes an exact count (every label with a positive
share at least once when the set allows), placed by the seeded shuffle or
spread evenly within groups so that cohorts stay balanced.
"""

import heapq
import itertools
import json
import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from flight_recorder.collector.canonical import canonical_hash
from flight_recorder.collector.schema import LogicArtifact, format_utc
from flight_recorder.dataset.schedule import Schedule, build_schedule
from flight_recorder.fixtures import canonical_envelope_paths
from flight_recorder.logic.evaluator import ContextInput, EvaluationResult, InputState, evaluate

__all__ = ["DatasetConfig", "DatasetConfigError", "generate"]

ACCOUNT_PREFIX = "ds-"

EMPLOYEE_COUNT = "employee_count"
INDUSTRY = "industry"
HEADQUARTERS_COUNTRY = "headquarters_country"
FUNDING_EVENT = "funding_event"
OPEN_ROLES = "open_platform_engineering_roles"
HEAD_OF_PLATFORM = "head_of_platform_start_date"
PRESSURE = "verified_integration_pressure"
WEBSITE_INTENT = "website_intent"
FIRMOGRAPHIC_KEYS = (EMPLOYEE_COUNT, INDUSTRY, HEADQUARTERS_COUNTRY, OPEN_ROLES)
ENRICHMENT_KEYS = (EMPLOYEE_COUNT, INDUSTRY, HEADQUARTERS_COUNTRY, FUNDING_EVENT, OPEN_ROLES)

FUNDING_STATES = ("recent", "stale", "absent", "unavailable")
PRESSURE_STATES = ("HIGH", "MEDIUM", "LOW", "absent", "unavailable")
ACTION_STATUSES = ("none", "failed", "sent", "completed")
ACTION_SHAPES = ("own_claim", "no_claim", "none")
FAILED_SHAPES = ("claim", "no_claim")
NO_ACTION_SHAPES = ("decision_reference", "reference_free", "none")
FROM_ACTION = frozenset({"own_claim", "no_claim", "claim"})
EXECUTED = frozenset({"sent", "completed"})

#: Kind order inside the generated sequence key `(account index, kind order, n)`.
KINDS = ("discovered", "evidence", "decision", "persona", "action", "outcome")

NAME_FRAGMENTS = (
    "Axo", "Brel", "Cyrr", "Dovex", "Elqu", "Fyra", "Gant", "Hexa", "Ivro", "Jexa",
    "Kvor", "Lumo", "Mavr", "Nexo", "Ovra", "Pelq", "Quor", "Ryst", "Sarn", "Tovu",
)  # fmt: skip
NAME_ENDINGS = (
    "loop", "grid", "forge", "vane", "path", "mesh", "lane", "stack", "wave",
    "field", "port", "line", "sync", "node", "rail", "craft", "core",
)  # fmt: skip
NAME_SUFFIXES = ("Labs", "Systems", "Software", "Cloud", "Data", "Networks", "Analytics")


# --- Config ---------------------------------------------------------------------------


class DatasetConfigError(ValueError):
    """A malformed dataset config, named by the field that is wrong."""

    def __init__(self, field_name: str, detail: str):
        super().__init__(f"dataset config field {field_name!r}: {detail}")
        self.field = field_name
        self.detail = detail


@dataclass(frozen=True)
class Option:
    """One evidence-value option: an inclusive integer range or a list of strings."""

    share: float
    low: int | None = None
    high: int | None = None
    values: tuple[str, ...] = ()


@dataclass(frozen=True)
class DecisionParameters:
    second_decision_share: float
    current_logic_share: float
    compared_workflow_share: float
    prioritize_intent_share: float
    boundary_margin_hours: int
    explanation_share: float
    explanations: tuple[str, ...]


@dataclass(frozen=True)
class EvidenceParameters:
    employee_count: tuple[Option, ...]
    industry: tuple[Option, ...]
    headquarters_country: tuple[Option, ...]
    open_platform_engineering_roles_share: float
    open_platform_engineering_roles: tuple[Option, ...]
    head_of_platform_share: float
    funding_mix: tuple[tuple[str, float], ...]
    funding_rounds: tuple[str, ...]
    funding_recent_days: tuple[int, int]
    funding_stale_days: tuple[int, int]
    pressure_mix: tuple[tuple[str, float], ...]
    pressure_basis: tuple[str, ...]
    website_intent_unavailable_share: float

    def options(self, key: str) -> tuple[Option, ...]:
        return getattr(self, key)


@dataclass(frozen=True)
class ActionParameters:
    status_mix: tuple[tuple[str, float], ...]
    plays: tuple[int, ...]
    personas: tuple[str, ...]
    cost_cents: tuple[int, int]


@dataclass(frozen=True)
class OutcomeParameters:
    action_observation_mix: tuple[tuple[str, float], ...]
    failed_action_observation_mix: tuple[tuple[str, float], ...]
    no_action_observation_mix: tuple[tuple[str, float], ...]
    period_mix: tuple[tuple[int, float], ...]
    closed_unknown_share: float
    open_age_days: tuple[int, int]
    open_unknown_share: float
    reply_share: float
    meeting_share: float
    recorded_delay_minutes: tuple[int, int]
    #: `(workflow_version, HIGH-pressure rate, other rate)`.
    opportunity_rates: tuple[tuple[str, float, float], ...]


@dataclass(frozen=True)
class StageTwoTarget:
    account_index: int
    #: The outcome number for a correction, the decision number for a new observation.
    n: int
    period_days: int
    opportunity: bool


@dataclass(frozen=True)
class DatasetConfig:
    seed: int
    account_count: int
    horizon: datetime
    attribution_instant: datetime
    comparison_workflow_version: str
    decisions: DecisionParameters
    evidence: EvidenceParameters
    actions: ActionParameters
    outcomes: OutcomeParameters
    correction: StageTwoTarget
    new_observation: StageTwoTarget

    @classmethod
    def from_mapping(cls, data: Mapping) -> "DatasetConfig":
        """Validate the config's shape and ranges; `DatasetConfigError` names the field."""
        root = _Fields(data, "")
        horizon = root.instant("horizon")
        attribution_instant = root.instant("attribution_instant")
        if attribution_instant < horizon:
            raise DatasetConfigError(
                "attribution_instant",
                f"{format_utc(attribution_instant)} is earlier than the horizon "
                f"{format_utc(horizon)}; A >= H",
            )
        comparison = root.text("comparison_workflow_version")
        compared = _CanonicalReference.load().workflow_version
        if comparison == compared:
            raise DatasetConfigError(
                "comparison_workflow_version",
                f"{comparison!r} is the workflow version the cohort is compared with",
            )

        d = root.section("decisions")
        decision_parameters = DecisionParameters(
            second_decision_share=d.share("second_decision_share"),
            current_logic_share=d.share("current_logic_share"),
            compared_workflow_share=d.share("compared_workflow_share"),
            prioritize_intent_share=d.share("prioritize_intent_share"),
            boundary_margin_hours=d.integer("boundary_margin_hours", minimum=6),
            explanation_share=d.share("explanation_share"),
            explanations=d.texts("explanations"),
        )
        e = root.section("evidence")
        evidence = EvidenceParameters(
            employee_count=e.options(EMPLOYEE_COUNT, integers=True),
            industry=e.options(INDUSTRY, integers=False),
            headquarters_country=e.options(HEADQUARTERS_COUNTRY, integers=False),
            open_platform_engineering_roles_share=e.share("open_platform_engineering_roles_share"),
            open_platform_engineering_roles=e.options(OPEN_ROLES, integers=True),
            head_of_platform_share=e.share("head_of_platform_share"),
            funding_mix=e.mix("funding_mix", FUNDING_STATES),
            funding_rounds=e.texts("funding_rounds"),
            funding_recent_days=e.pair("funding_recent_days", minimum=1),
            funding_stale_days=e.pair("funding_stale_days", minimum=1),
            pressure_mix=e.mix("pressure_mix", PRESSURE_STATES),
            pressure_basis=e.texts("pressure_basis"),
            website_intent_unavailable_share=e.share("website_intent_unavailable_share"),
        )
        a = root.section("actions")
        actions = ActionParameters(
            status_mix=a.mix("status_mix", ACTION_STATUSES),
            plays=a.integers("plays"),
            personas=a.texts("personas"),
            cost_cents=a.pair("cost_cents", minimum=1),
        )
        o = root.section("outcomes")
        try:
            period_days = tuple(sorted(int(key) for key in o.mapping("period_mix")))
        except ValueError as error:
            raise DatasetConfigError("outcomes.period_mix", "keys must be day counts") from error
        if not period_days or period_days[0] < 1:
            raise DatasetConfigError("outcomes.period_mix", "needs positive day counts")
        period_mix = o.mix("period_mix", tuple(str(days) for days in period_days))
        rates = o.section("opportunity_rates")
        if set(rates.data) != {compared, comparison}:
            raise DatasetConfigError(
                "outcomes.opportunity_rates",
                f"must name exactly the workflow versions {compared!r} and {comparison!r}",
            )
        outcome_parameters = OutcomeParameters(
            action_observation_mix=o.mix("action_observation_mix", ACTION_SHAPES),
            failed_action_observation_mix=o.mix("failed_action_observation_mix", FAILED_SHAPES),
            no_action_observation_mix=o.mix("no_action_observation_mix", NO_ACTION_SHAPES),
            period_mix=tuple((int(label), share) for label, share in period_mix),
            closed_unknown_share=o.share("closed_unknown_share"),
            open_age_days=o.pair("open_age_days", minimum=1),
            open_unknown_share=o.share("open_unknown_share"),
            reply_share=o.share("reply_share"),
            meeting_share=o.share("meeting_share"),
            recorded_delay_minutes=o.pair("recorded_delay_minutes", minimum=1),
            opportunity_rates=tuple(
                (
                    version,
                    rates.section(version).share("HIGH"),
                    rates.section(version).share("other"),
                )
                for version in (compared, comparison)
            ),
        )
        s = root.section("stage_2")
        correction = s.section("correction")
        new_observation = s.section("new_observation")
        return cls(
            seed=root.integer("seed"),
            account_count=root.integer("account_count", minimum=1),
            horizon=horizon,
            attribution_instant=attribution_instant,
            comparison_workflow_version=comparison,
            decisions=decision_parameters,
            evidence=evidence,
            actions=actions,
            outcomes=outcome_parameters,
            correction=StageTwoTarget(
                account_index=correction.integer("account_index", minimum=1),
                n=correction.integer("outcome_n", minimum=1),
                period_days=correction.integer("period_days", minimum=1),
                opportunity=correction.boolean("opportunity"),
            ),
            new_observation=StageTwoTarget(
                account_index=new_observation.integer("account_index", minimum=1),
                n=new_observation.integer("decision_n", minimum=1),
                period_days=new_observation.integer("period_days", minimum=1),
                opportunity=new_observation.boolean("opportunity"),
            ),
        )


class _Fields:
    """Typed reads from one config section, each failure named by its dotted field."""

    def __init__(self, data, path: str):
        if not isinstance(data, Mapping):
            raise DatasetConfigError(path or "config", "must be an object")
        self.data = data
        self.path = path

    def name(self, key: str) -> str:
        return f"{self.path}.{key}" if self.path else key

    def raw(self, key: str):
        if key not in self.data:
            raise DatasetConfigError(self.name(key), "is missing")
        return self.data[key]

    def section(self, key: str) -> "_Fields":
        return _Fields(self.raw(key), self.name(key))

    def mapping(self, key: str) -> Mapping:
        return self.section(key).data

    def integer(self, key: str, *, minimum: int | None = None) -> int:
        value = self.raw(key)
        if type(value) is not int:
            raise DatasetConfigError(self.name(key), f"must be an integer, not {value!r}")
        if minimum is not None and value < minimum:
            raise DatasetConfigError(self.name(key), f"must be at least {minimum}")
        return value

    def boolean(self, key: str) -> bool:
        value = self.raw(key)
        if type(value) is not bool:
            raise DatasetConfigError(self.name(key), f"must be true or false, not {value!r}")
        return value

    def share(self, key: str) -> float:
        value = self.raw(key)
        if type(value) not in (int, float) or not 0 <= value <= 1:
            raise DatasetConfigError(self.name(key), f"must be a share in [0, 1], not {value!r}")
        return float(value)

    def text(self, key: str) -> str:
        value = self.raw(key)
        if not isinstance(value, str) or not value:
            raise DatasetConfigError(self.name(key), "must be a non-empty string")
        return value

    def texts(self, key: str) -> tuple[str, ...]:
        value = self.raw(key)
        if (
            not isinstance(value, list)
            or not value
            or not all(isinstance(v, str) and v for v in value)
        ):
            raise DatasetConfigError(self.name(key), "must be a non-empty list of strings")
        return tuple(value)

    def integers(self, key: str) -> tuple[int, ...]:
        value = self.raw(key)
        if not isinstance(value, list) or not value or not all(type(v) is int for v in value):
            raise DatasetConfigError(self.name(key), "must be a non-empty list of integers")
        return tuple(value)

    def pair(self, key: str, *, minimum: int) -> tuple[int, int]:
        value = self.raw(key)
        if (
            not isinstance(value, list)
            or len(value) != 2
            or not all(type(v) is int for v in value)
            or not minimum <= value[0] <= value[1]
        ):
            raise DatasetConfigError(
                self.name(key), f"must be [low, high] integers with {minimum} <= low <= high"
            )
        return value[0], value[1]

    def instant(self, key: str) -> datetime:
        value = self.raw(key)
        try:
            parsed = datetime.fromisoformat(value) if isinstance(value, str) else None
        except ValueError:
            parsed = None
        if parsed is None or parsed.tzinfo is None:
            raise DatasetConfigError(self.name(key), f"must be a UTC instant, not {value!r}")
        return parsed.astimezone(UTC)

    def mix(self, key: str, labels: Sequence[str]) -> tuple[tuple[str, float], ...]:
        section = self.section(key)
        if set(section.data) != set(labels):
            raise DatasetConfigError(self.name(key), f"must have exactly the keys {list(labels)}")
        mix = tuple((label, section.share(label)) for label in labels)
        if not math.isclose(sum(share for _, share in mix), 1.0, abs_tol=1e-9):
            raise DatasetConfigError(self.name(key), "shares must sum to 1")
        return mix

    def options(self, key: str, *, integers: bool) -> tuple[Option, ...]:
        value = self.raw(key)
        name = self.name(key)
        if not isinstance(value, list) or not value:
            raise DatasetConfigError(name, "must be a non-empty list of options")
        options = []
        for position, raw in enumerate(value):
            entry = _Fields(raw, f"{name}[{position}]")
            share = entry.share("share")
            if integers:
                low, high = entry.pair("range", minimum=0)
                options.append(Option(share=share, low=low, high=high))
            else:
                options.append(Option(share=share, values=entry.texts("values")))
        if not math.isclose(sum(option.share for option in options), 1.0, abs_tol=1e-9):
            raise DatasetConfigError(name, "option shares must sum to 1")
        return tuple(options)


# --- The canonical reference ------------------------------------------------------------


@dataclass(frozen=True)
class _CanonicalReference:
    """What the generator reads from the canonical envelopes rather than restating."""

    envelopes: tuple[bytes, ...]
    workflow_version: str
    sources: Mapping[str, str]
    enrichment_source: str
    research_source: str
    action_type: str
    currency: str

    @classmethod
    def load(cls) -> "_CanonicalReference":
        bodies = tuple(path.read_bytes() for path in canonical_envelope_paths())
        parsed = [json.loads(body) for body in bodies]
        by_type = {envelope["event_type"]: envelope for envelope in parsed}

        def carrying(key: str) -> str:
            return next(
                envelope["source"]
                for envelope in parsed
                if envelope["event_type"] == "evidence.recorded"
                and any(item["evidence_type"] == key for item in envelope["payload"]["items"])
            )

        action = by_type["action.recorded"]["payload"]
        return cls(
            envelopes=bodies,
            workflow_version=by_type["decision.recorded"]["payload"]["workflow_version"],
            sources={event_type: envelope["source"] for event_type, envelope in by_type.items()},
            enrichment_source=carrying(EMPLOYEE_COUNT),
            research_source=carrying(PRESSURE),
            action_type=action["action_type"],
            currency=action["currency"],
        )


# --- Decks ------------------------------------------------------------------------------


def _counted(
    n: int, mix: Sequence[tuple[object, float]], at_least: Mapping[object, int] | None = None
) -> list[tuple[object, int]]:
    """Exact counts for `n` units by largest remainder. Every label with a positive
    share gets at least one (or its `at_least`) when `n` allows all of them."""
    exact = [share * n for _, share in mix]
    counts = [math.floor(x) for x in exact]
    positive = [i for i, (_, share) in enumerate(mix) if share > 0]
    floors = [0] * len(mix)
    wanted = {i: max(1, (at_least or {}).get(mix[i][0], 1)) for i in positive}
    if n >= sum(wanted.values()):
        for i, minimum in wanted.items():
            floors[i] = minimum
            counts[i] = max(counts[i], minimum)
    while sum(counts) > n:
        reducible = [i for i in range(len(mix)) if counts[i] > floors[i]]
        i = max(reducible, key=lambda j: (counts[j] - exact[j], -j))
        counts[i] -= 1
    order = sorted(positive, key=lambda j: (-(exact[j] - counts[j]), j))
    position = 0
    while sum(counts) < n:
        counts[order[position % len(order)]] += 1
        position += 1
    return [(label, count) for (label, _), count in zip(mix, counts, strict=True)]


def _deck(rng: random.Random, n: int, mix, at_least=None) -> list:
    labels = [label for label, count in _counted(n, mix, at_least) for _ in range(count)]
    rng.shuffle(labels)
    return labels


def _spread(counted: Sequence[tuple[object, int]]) -> list:
    """Exact counts laid out evenly, so a contiguous block holds each label in proportion."""
    n = sum(count for _, count in counted)
    used = [0] * len(counted)
    labels = []
    for position in range(n):
        j = max(
            (j for j in range(len(counted)) if used[j] < counted[j][1]),
            key=lambda j: (counted[j][1] * (position + 1) - used[j] * n, -j),
        )
        used[j] += 1
        labels.append(counted[j][0])
    return labels


def _grouped(rng: random.Random, units: Sequence, group, counted) -> list:
    """Labels for `units`, spread evenly inside each group in a seeded order."""
    tiebreak = [rng.random() for _ in units]
    order = sorted(range(len(units)), key=lambda i: (group(units[i]), tiebreak[i]))
    labels = [None] * len(units)
    for position, label in zip(order, _spread(counted), strict=True):
        labels[position] = label
    return labels


def _binary(share: float) -> tuple[tuple[bool, float], ...]:
    return ((True, share), (False, 1 - share))


def _seconds(rng: random.Random, low: int, high: int) -> timedelta:
    return timedelta(seconds=rng.randint(low, high))


# --- Plans -------------------------------------------------------------------------------


@dataclass
class _Evidence:
    key: str
    version_id: str
    value: object
    observed_at: date | None = None
    basis: tuple[str, ...] = ()


@dataclass
class _Observation:
    decision: "_Decision"
    n: int
    shape: str
    period_days: int
    opened: datetime
    closes: datetime
    closed: bool
    observed: datetime
    recorded: datetime
    source_action: str | None
    source_decision: str | None
    unknown: bool = False
    opportunity: bool | None = None
    reply: bool | None = None
    meeting: bool | None = None
    supersedes: str | None = None

    @property
    def event_id(self) -> str:
        return f"{self.decision.account.ref}-outcome-{self.n}"

    @property
    def planned_qualifying(self) -> bool:
        """Closed, known, 90 days, and planned to be attributed to its own decision."""
        if not self.closed or self.unknown or self.period_days != 90:
            return False
        if self.shape == "decision_reference":
            return True
        return self.shape in ("own_claim", "no_claim") and self.decision.action_status in EXECUTED


@dataclass
class _Decision:
    account: "_Account"
    n: int
    artifact: LogicArtifact
    artifact_hash: str
    boundary: datetime
    workflow: str = ""
    recorded: datetime | None = None
    explanation: str | None = None
    context: tuple[ContextInput, ...] = ()
    result: EvaluationResult | None = None
    persona: str | None = None
    persona_at: datetime | None = None
    action_status: str | None = None
    action_at: datetime | None = None
    play: int | None = None
    cost: str | None = None
    observation: _Observation | None = None

    @property
    def event_id(self) -> str:
        return f"{self.account.ref}-decision-{self.n}"

    @property
    def persona_id(self) -> str:
        return f"{self.account.ref}-persona-{self.n}"

    @property
    def action_id(self) -> str:
        return f"{self.account.ref}-action-{self.n}"


@dataclass
class _Account:
    index: int
    name: str
    domain: str
    funding: str
    roles_present: bool
    head_of_platform: bool
    website_intent_unavailable: bool
    pressure: str = ""
    evidence: dict[str, _Evidence] = field(default_factory=dict)
    decisions: list[_Decision] = field(default_factory=list)
    discovered_at: datetime | None = None
    enrichment_at: datetime | None = None
    research_at: datetime | None = None
    outcome_count: int = 0

    @property
    def ref(self) -> str:
        return f"{ACCOUNT_PREFIX}{self.index:04d}"

    @property
    def has_executed_action(self) -> bool:
        return any(decision.action_status in EXECUTED for decision in self.decisions)


@dataclass
class _Plan:
    config: DatasetConfig
    rng: random.Random
    reference: _CanonicalReference
    earlier: LogicArtifact
    later: LogicArtifact
    accounts: list[_Account] = field(default_factory=list)

    @property
    def decisions(self) -> list[_Decision]:
        return [decision for account in self.accounts for decision in account.decisions]


# --- Planning ----------------------------------------------------------------------------


def _artifact_pair(artifacts: Mapping[str, LogicArtifact]) -> tuple[LogicArtifact, LogicArtifact]:
    ordered = sorted(artifacts.values(), key=lambda artifact: artifact.activation.activated_at)
    if len(ordered) != 2:
        raise ValueError(f"the generator selects between two artifacts, not {len(ordered)}")
    earlier, later = ordered
    if earlier.activation.deactivated_at is None:
        raise ValueError(f"{earlier.logic_version} records no deactivation instant")
    return earlier, later


def _artifact_hash(artifact: LogicArtifact) -> str:
    return canonical_hash(artifact.model_dump(mode="json"))


def _plan(config: DatasetConfig, artifacts: Mapping[str, LogicArtifact]) -> _Plan:
    earlier, later = _artifact_pair(artifacts)
    plan = _Plan(
        config=config,
        rng=random.Random(config.seed),
        reference=_CanonicalReference.load(),
        earlier=earlier,
        later=later,
    )
    _plan_accounts(plan)
    _plan_decisions(plan)
    _plan_evidence(plan)
    _plan_actions(plan)
    _plan_observations(plan)
    _plan_opportunities(plan)
    return plan


def _plan_accounts(plan: _Plan) -> None:
    rng, evidence, n = plan.rng, plan.config.evidence, plan.config.account_count
    names = rng.sample(list(itertools.product(NAME_FRAGMENTS, NAME_ENDINGS, NAME_SUFFIXES)), n)
    funding = _deck(rng, n, evidence.funding_mix)
    roles = _deck(rng, n, _binary(evidence.open_platform_engineering_roles_share))
    head_of_platform = _deck(rng, n, _binary(evidence.head_of_platform_share))
    website = _deck(rng, n, _binary(evidence.website_intent_unavailable_share))
    for position, (fragment, ending, suffix) in enumerate(names):
        stem = f"{fragment}{ending}"
        plan.accounts.append(
            _Account(
                index=position + 1,
                name=f"{stem} {suffix}",
                domain=f"{stem.lower()}-{suffix.lower()}.example",
                funding=funding[position],
                roles_present=roles[position],
                head_of_platform=head_of_platform[position],
                website_intent_unavailable=website[position],
            )
        )
    pressure = _grouped(
        rng, plan.accounts, lambda account: account.funding, _counted(n, evidence.pressure_mix)
    )
    for account, state in zip(plan.accounts, pressure, strict=True):
        account.pressure = state


def _plan_decisions(plan: _Plan) -> None:
    rng, config = plan.rng, plan.config
    parameters = config.decisions
    accounts = plan.accounts
    both = _deck(rng, len(accounts), _binary(parameters.second_decision_share))
    singles = [account for account, two in zip(accounts, both, strict=True) if not two]
    single_later = dict(
        zip(
            (account.index for account in singles),
            _deck(rng, len(singles), _binary(parameters.current_logic_share)),
            strict=True,
        )
    )

    earlier_start = plan.earlier.activation.activated_at
    earlier_end = plan.earlier.activation.deactivated_at
    later_start = plan.later.activation.activated_at
    later_end = config.horizon - timedelta(hours=parameters.boundary_margin_hours)
    if not earlier_start < earlier_end <= later_start < later_end:
        raise DatasetConfigError(
            "horizon", "leaves no boundary window after the later artifact's activation"
        )

    def boundary(start: datetime, end: datetime) -> datetime:
        return start + timedelta(seconds=rng.randrange(int((end - start).total_seconds())))

    for account, two in zip(accounts, both, strict=True):
        for artifact, wanted in (
            (plan.earlier, two or not single_later[account.index]),
            (plan.later, two or single_later[account.index]),
        ):
            if not wanted:
                continue
            start, end = (
                (earlier_start, earlier_end)
                if artifact is plan.earlier
                else (later_start, later_end)
            )
            account.decisions.append(
                _Decision(
                    account=account,
                    n=len(account.decisions) + 1,
                    artifact=artifact,
                    artifact_hash=_artifact_hash(artifact),
                    boundary=boundary(start, end),
                )
            )

    # The boundaries on either side of the later activation, on single-decision accounts.
    edge_earlier = next((a.decisions[0] for a in singles if not single_later[a.index]), None)
    edge_later = next((a.decisions[0] for a in singles if single_later[a.index]), None)
    if edge_earlier is None or edge_later is None:
        raise DatasetConfigError(
            "decisions.current_logic_share",
            "needs single-decision accounts under both artifacts for the activation boundaries",
        )
    edge_earlier.boundary = later_start - timedelta(microseconds=1)
    edge_later.boundary = later_start

    decisions = plan.decisions
    workflows = _grouped(
        rng,
        decisions,
        lambda d: (d.account.funding, d.account.pressure == "HIGH"),
        _counted(
            len(decisions),
            (
                (plan.reference.workflow_version, parameters.compared_workflow_share),
                (config.comparison_workflow_version, 1 - parameters.compared_workflow_share),
            ),
        ),
    )
    explained = _deck(rng, len(decisions), _binary(parameters.explanation_share))
    for decision, workflow, explain in zip(decisions, workflows, explained, strict=True):
        decision.workflow = workflow
        decision.recorded = decision.boundary + _seconds(rng, 1, 30)
        decision.explanation = rng.choice(parameters.explanations) if explain else None


def _version_id(account: _Account, key: str) -> str:
    return f"{account.ref}-{key.replace('_', '-')}-v1"


def _apply_firmographics(plan: _Plan, account: _Account, chosen: Mapping[str, int | None]) -> None:
    for key in FIRMOGRAPHIC_KEYS:
        index = chosen[key]
        if index is None:
            account.evidence.pop(key, None)
            continue
        option = plan.config.evidence.options(key)[index]
        value = (
            plan.rng.choice(option.values)
            if option.values
            else plan.rng.randint(option.low, option.high)
        )
        account.evidence[key] = _Evidence(key, _version_id(account, key), value)


def _search_firmographics(plan: _Plan, account: _Account, drawn, want: str) -> None:
    """Choose firmographic options whose evaluation gives `want`, starting from the
    drawn options; restore the drawn options when no combination does."""
    first = account.decisions[0]

    def output() -> str:
        return evaluate(first.artifact, _context(account), first.boundary).output

    if output() == want:
        return
    evidence = plan.config.evidence
    ranges = [range(len(evidence.options(key))) for key in FIRMOGRAPHIC_KEYS[:3]]
    ranges.append(range(len(evidence.options(OPEN_ROLES))) if account.roles_present else [None])
    combinations = list(itertools.product(*ranges))
    plan.rng.shuffle(combinations)
    for combination in combinations:
        _apply_firmographics(plan, account, dict(zip(FIRMOGRAPHIC_KEYS, combination, strict=True)))
        if output() == want:
            return
    _apply_firmographics(plan, account, drawn)


def _context(account: _Account) -> tuple[ContextInput, ...]:
    """`H(d)` for a decision of this account: every key with evidence, plus the
    keys the account records as unavailable. Absent keys have no entry."""
    entries = [
        ContextInput(
            key=item.key,
            availability="available",
            value=item.value,
            evidence_version_id=item.version_id,
            observed_at=item.observed_at,
        )
        for item in account.evidence.values()
    ]
    if account.funding == "unavailable":
        entries.append(ContextInput(key=FUNDING_EVENT, availability="unavailable"))
    if account.pressure == "unavailable":
        entries.append(ContextInput(key=PRESSURE, availability="unavailable"))
    if account.website_intent_unavailable:
        entries.append(ContextInput(key=WEBSITE_INTENT, availability="unavailable"))
    return tuple(sorted(entries, key=lambda entry: entry.key))


def _plan_evidence(plan: _Plan) -> None:
    rng, evidence, accounts = plan.rng, plan.config.evidence, plan.accounts
    option_decks = {
        key: _deck(
            rng,
            len(accounts),
            tuple((index, option.share) for index, option in enumerate(evidence.options(key))),
        )
        for key in FIRMOGRAPHIC_KEYS
    }
    searched = [
        account
        for account in accounts
        if account.funding == "recent" and account.decisions[0].artifact is plan.earlier
    ]
    intent = dict(
        zip(
            (account.index for account in searched),
            _deck(rng, len(searched), _binary(plan.config.decisions.prioritize_intent_share)),
            strict=True,
        )
    )
    for position, account in enumerate(accounts):
        first = account.decisions[0]
        day = first.boundary.date()
        if account.funding in ("recent", "stale"):
            low, high = (
                evidence.funding_recent_days
                if account.funding == "recent"
                else evidence.funding_stale_days
            )
            account.evidence[FUNDING_EVENT] = _Evidence(
                FUNDING_EVENT,
                _version_id(account, FUNDING_EVENT),
                rng.choice(evidence.funding_rounds),
                observed_at=day - timedelta(days=rng.randint(low, high)),
            )
        if account.head_of_platform:
            started = day - timedelta(days=rng.randint(5, 400))
            account.evidence[HEAD_OF_PLATFORM] = _Evidence(
                HEAD_OF_PLATFORM,
                _version_id(account, HEAD_OF_PLATFORM),
                started.isoformat(),
                observed_at=started,
            )
        if account.pressure in ("HIGH", "MEDIUM", "LOW"):
            account.evidence[PRESSURE] = _Evidence(
                PRESSURE,
                _version_id(account, PRESSURE),
                account.pressure,
                basis=tuple(rng.sample(evidence.pressure_basis, rng.randint(1, 3))),
            )
        drawn = {key: option_decks[key][position] for key in FIRMOGRAPHIC_KEYS}
        if not account.roles_present:
            drawn[OPEN_ROLES] = None
        _apply_firmographics(plan, account, drawn)
        if account.index in intent:
            mapping = first.artifact.output_mapping
            want = (
                mapping.at_or_above_threshold if intent[account.index] else mapping.below_threshold
            )
            _search_firmographics(plan, account, drawn, want)

        account.enrichment_at = first.boundary - _seconds(rng, 30 * 60, 20 * 3600)
        account.research_at = first.boundary - _seconds(rng, 60, 25 * 60)
        account.discovered_at = account.enrichment_at - _seconds(rng, 2 * 3600, 45 * 86400)

    for decision in plan.decisions:
        decision.context = _context(decision.account)
        decision.result = evaluate(decision.artifact, decision.context, decision.boundary)


def _plan_actions(plan: _Plan) -> None:
    rng, parameters = plan.rng, plan.config.actions
    prioritized = [
        d
        for d in plan.decisions
        if d.result.output == d.artifact.output_mapping.at_or_above_threshold
    ]
    statuses = _deck(rng, len(prioritized), parameters.status_mix, at_least={"failed": 2})
    for decision, status in zip(prioritized, statuses, strict=True):
        decision.persona = rng.choice(parameters.personas)
        decision.persona_at = decision.boundary + _seconds(rng, 30, 300)
        if status == "none":
            continue
        decision.action_status = status
        decision.action_at = decision.persona_at + _seconds(rng, 60, 3 * 3600)
        decision.play = rng.choice(parameters.plays)
        cents = rng.randint(*parameters.cost_cents)
        decision.cost = f"{cents // 100}.{cents % 100:02d}"


def _observation(plan: _Plan, decision: _Decision, shape: str, period_days: int) -> _Observation:
    rng, outcomes, horizon = plan.rng, plan.config.outcomes, plan.config.horizon
    opened = decision.action_at if shape in FROM_ACTION else decision.boundary
    closes = opened + timedelta(days=period_days)
    closed = closes <= horizon
    if closed:
        observed = closes
    else:
        observed = min(opened + timedelta(days=rng.randint(*outcomes.open_age_days)), horizon)
    decision.account.outcome_count += 1
    return _Observation(
        decision=decision,
        n=decision.account.outcome_count,
        shape=shape,
        period_days=period_days,
        opened=opened,
        closes=closes,
        closed=closed,
        observed=observed,
        recorded=observed + timedelta(minutes=rng.randint(*outcomes.recorded_delay_minutes)),
        source_action=decision.action_id if shape in ("own_claim", "claim") else None,
        source_decision=decision.event_id if shape == "decision_reference" else None,
    )


def _plan_observations(plan: _Plan) -> None:
    rng, outcomes, decisions = plan.rng, plan.config.outcomes, plan.decisions
    no_action = [d for d in decisions if d.action_status is None]
    shares = dict(outcomes.no_action_observation_mix)
    pools = (
        ([d for d in decisions if d.action_status in EXECUTED], outcomes.action_observation_mix),
        (
            [d for d in decisions if d.action_status == "failed"],
            outcomes.failed_action_observation_mix,
        ),
        # Reference-free observations only where the account holds no executed action.
        (
            [d for d in no_action if not d.account.has_executed_action],
            outcomes.no_action_observation_mix,
        ),
        (
            [d for d in no_action if d.account.has_executed_action],
            (
                ("decision_reference", shares["decision_reference"] + shares["reference_free"]),
                ("none", shares["none"]),
            ),
        ),
    )
    shape_of: dict[str, str] = {}
    for pool, mix in pools:
        for decision, shape in zip(pool, _deck(rng, len(pool), mix), strict=True):
            shape_of[decision.event_id] = shape

    observed = [d for d in decisions if shape_of[d.event_id] != "none"]
    periods = _deck(rng, len(observed), outcomes.period_mix)
    for decision, period_days in zip(observed, periods, strict=True):
        decision.observation = _observation(
            plan, decision, shape_of[decision.event_id], period_days
        )

    closed = [d.observation for d in observed if d.observation.closed]
    unknown = _deck(rng, len(closed), _binary(outcomes.closed_unknown_share))
    for observation, is_unknown in zip(closed, unknown, strict=True):
        observation.unknown = is_unknown


def _rate(plan: _Plan, decision: _Decision) -> float:
    for workflow, high, other in plan.config.outcomes.opportunity_rates:
        if workflow == decision.workflow:
            return high if decision.account.pressure == "HIGH" else other
    raise AssertionError(f"no opportunity rate for {decision.workflow!r}")  # pragma: no cover


def _plan_opportunities(plan: _Plan) -> None:
    rng, outcomes = plan.rng, plan.config.outcomes
    observations = [d.observation for d in plan.decisions if d.observation is not None]

    # Planned-qualifying observations: an exact quota per workflow and pressure group,
    # spread evenly across funding groups inside it.
    cells: dict[tuple[str, bool], list[_Observation]] = {}
    for observation in observations:
        if observation.planned_qualifying:
            decision = observation.decision
            key = (decision.workflow, decision.account.pressure == "HIGH")
            cells.setdefault(key, []).append(observation)
    for key in sorted(cells):
        members = cells[key]
        positives = math.floor(_rate(plan, members[0].decision) * len(members) + 0.5)
        labels = _grouped(
            rng,
            members,
            lambda o: (o.decision.account.funding, o.decision.n),
            [(True, positives), (False, len(members) - positives)],
        )
        for observation, value in zip(members, labels, strict=True):
            observation.opportunity = value

    for observation in observations:
        rate = _rate(plan, observation.decision)
        if observation.closed and not observation.unknown and not observation.planned_qualifying:
            observation.opportunity = rng.random() < rate
        elif not observation.closed:
            unknown = rng.random() < outcomes.open_unknown_share
            observation.opportunity = None if unknown else rng.random() < rate
        if observation.opportunity:
            observation.reply = observation.meeting = True
        else:
            observation.reply = rng.random() < outcomes.reply_share
            observation.meeting = observation.reply and rng.random() < outcomes.meeting_share
        if not observation.closed and rng.random() < outcomes.open_unknown_share:
            observation.meeting = None


def _stage_two(plan: _Plan) -> tuple[_Observation, _Observation]:
    config = plan.config
    target = config.correction
    if not 1 <= target.account_index <= len(plan.accounts):
        raise DatasetConfigError("stage_2.correction.account_index", "names no generated account")
    account = plan.accounts[target.account_index - 1]
    predecessor = next(
        (
            d.observation
            for d in account.decisions
            if d.observation is not None and d.observation.n == target.n
        ),
        None,
    )
    if predecessor is None or predecessor.closed:
        raise DatasetConfigError(
            "stage_2.correction.outcome_n",
            f"{account.ref} outcome {target.n} is not a stage-1 open observation",
        )
    closes = predecessor.opened + timedelta(days=target.period_days)
    if closes > config.horizon:
        raise DatasetConfigError(
            "stage_2.correction.period_days",
            f"a {target.period_days}-day period from {format_utc(predecessor.opened)} closes "
            "after the horizon, and every observed_at is bounded by it",
        )
    delay = config.outcomes.recorded_delay_minutes
    account.outcome_count += 1
    correction = _Observation(
        decision=predecessor.decision,
        n=account.outcome_count,
        shape=predecessor.shape,
        period_days=target.period_days,
        opened=predecessor.opened,
        closes=closes,
        closed=True,
        observed=closes,
        recorded=config.attribution_instant + timedelta(minutes=plan.rng.randint(*delay)),
        source_action=predecessor.source_action,
        source_decision=predecessor.source_decision,
        opportunity=target.opportunity,
        reply=target.opportunity,
        meeting=target.opportunity,
        supersedes=predecessor.event_id,
    )

    target = config.new_observation
    if not 1 <= target.account_index <= len(plan.accounts):
        raise DatasetConfigError(
            "stage_2.new_observation.account_index", "names no generated account"
        )
    account = plan.accounts[target.account_index - 1]
    if account is correction.decision.account:
        raise DatasetConfigError(
            "stage_2.new_observation.account_index", "is the corrected observation's account"
        )
    if not 1 <= target.n <= len(account.decisions):
        raise DatasetConfigError(
            "stage_2.new_observation.decision_n", f"{account.ref} has no decision {target.n}"
        )
    decision = account.decisions[target.n - 1]
    if decision.observation is not None:
        raise DatasetConfigError(
            "stage_2.new_observation.decision_n",
            f"{decision.event_id} already has a stage-1 observation",
        )
    shape = "no_claim" if decision.action_status in EXECUTED else "decision_reference"
    new = _observation(plan, decision, shape, target.period_days)
    new.recorded = config.attribution_instant + timedelta(minutes=plan.rng.randint(*delay))
    new.opportunity = new.reply = new.meeting = target.opportunity
    return correction, new


# --- Envelopes ---------------------------------------------------------------------------


def _envelope(event_id, event_type, source, account_ref, occurred, recorded, payload) -> dict:
    return {
        "schema_version": "1",
        "event_id": event_id,
        "event_type": event_type,
        "source": source,
        "account_ref": account_ref,
        "occurred_at": format_utc(occurred),
        "recorded_at": format_utc(recorded),
        "payload": payload,
    }


def _evidence_item(item: _Evidence) -> dict:
    body = {"evidence_version_id": item.version_id, "evidence_type": item.key, "value": item.value}
    if item.observed_at is not None:
        body["observed_at"] = item.observed_at.isoformat()
    if item.basis:
        body["basis"] = list(item.basis)
    return body


def _outcome_envelope(plan: _Plan, observation: _Observation) -> dict:
    payload = {
        "window_opened_at": format_utc(observation.opened),
        "window_closes_at": format_utc(observation.closes),
        "evaluation_state": "closed" if observation.closed else "open",
        "observed_at": format_utc(observation.observed),
    }
    # An unknown value is omitted, which schema v2 reads as unknown.
    if observation.reply is not None:
        payload["reply"] = observation.reply
    if observation.meeting is not None:
        payload["meeting"] = observation.meeting
    if observation.opportunity is not None and not observation.unknown:
        payload["opportunity"] = observation.opportunity
    if observation.source_action is not None:
        payload["source_action_event_id"] = observation.source_action
    if observation.source_decision is not None:
        payload["source_decision_event_id"] = observation.source_decision
    if observation.supersedes is not None:
        payload["supersedes_outcome_event_id"] = observation.supersedes
    envelope = _envelope(
        observation.event_id,
        "outcome.evaluated",
        plan.reference.sources["outcome.evaluated"],
        observation.decision.account.ref,
        observation.observed,
        observation.recorded,
        payload,
    )
    envelope["schema_version"] = "2"
    return envelope


def _decision_envelope(plan: _Plan, decision: _Decision) -> dict:
    result = decision.result
    values = {entry.key: entry.value for entry in decision.context}
    historical = [
        {
            "input_key": entry.key,
            "value": entry.value,
            "availability": "available",
            "evidence_version_id": entry.evidence_version_id,
        }
        if entry.is_available
        else {"input_key": entry.key, "value": None, "availability": "unavailable"}
        for entry in decision.context
    ]
    consumed = [
        {
            "input_key": factor.key,
            "value": values[factor.key],
            "evidence_version_id": factor.evidence_version_id,
            "contribution": factor.contribution,
        }
        for factor in result.factors
        if factor.input_state is InputState.CONSUMED
    ]
    return _envelope(
        decision.event_id,
        "decision.recorded",
        plan.reference.sources["decision.recorded"],
        decision.account.ref,
        decision.boundary,
        decision.recorded,
        {
            "decision_class": decision.artifact.decision_class,
            "decision_boundary": format_utc(decision.boundary),
            "workflow_version": decision.workflow,
            "historical_context": historical,
            "consumed_inputs": consumed,
            "logic_artifact": {
                "logic_version": decision.artifact.logic_version,
                "artifact_id": decision.artifact.artifact_id,
                "artifact_hash": decision.artifact_hash,
                "evaluator_version": decision.artifact.evaluator_version,
            },
            "result": {
                "score": result.score,
                "threshold": result.threshold,
                "output": result.output,
            },
            "explanation": decision.explanation,
        },
    )


class _Graph:
    """Envelopes with their prerequisites, released in dependency order and, among
    those ready, by `occurred_at` and then the generated sequence key."""

    def __init__(self):
        self.nodes: dict[str, tuple[str, tuple[int, int, int], dict]] = {}
        self.requires: dict[str, tuple[str, ...]] = {}

    def add(self, envelope: dict, kind: str, n: int, index: int, needs: Sequence[str]) -> None:
        key = (index, KINDS.index(kind), n)
        self.nodes[envelope["event_id"]] = (envelope["occurred_at"], key, envelope)
        self.requires[envelope["event_id"]] = tuple(needs)

    def ordered(self) -> list[dict]:
        waiting = {event_id: len(needs) for event_id, needs in self.requires.items()}
        dependents: dict[str, list[str]] = {event_id: [] for event_id in self.requires}
        for event_id, needs in self.requires.items():
            for need in needs:
                dependents[need].append(event_id)
        ready = [(*self.nodes[e][:2], e) for e, count in waiting.items() if count == 0]
        heapq.heapify(ready)
        released = []
        while ready:
            *_, event_id = heapq.heappop(ready)
            released.append(self.nodes[event_id][2])
            for dependent in dependents[event_id]:
                waiting[dependent] -= 1
                if waiting[dependent] == 0:
                    heapq.heappush(ready, (*self.nodes[dependent][:2], dependent))
        if len(released) != len(self.nodes):  # pragma: no cover - acyclic by construction
            raise AssertionError("stage-1 dependencies contain a cycle")
        return released


def _stage_one(plan: _Plan) -> list[dict]:
    rng, reference = plan.rng, plan.reference
    graph = _Graph()
    for account in plan.accounts:
        ref, index = account.ref, account.index
        discovered = f"{ref}-discovered-1"
        graph.add(
            _envelope(
                discovered,
                "account.discovered",
                reference.sources["account.discovered"],
                ref,
                account.discovered_at,
                account.discovered_at + _seconds(rng, 1, 300),
                {"name": account.name, "domain": account.domain},
            ),
            "discovered",
            1,
            index,
            (),
        )
        evidence_events = []
        for source, recorded, keys in (
            (
                reference.enrichment_source,
                account.enrichment_at,
                (*ENRICHMENT_KEYS, HEAD_OF_PLATFORM),
            ),
            (reference.research_source, account.research_at, (PRESSURE,)),
        ):
            items = [account.evidence[key] for key in keys if key in account.evidence]
            if not items:
                continue
            event_id = f"{ref}-evidence-{len(evidence_events) + 1}"
            graph.add(
                _envelope(
                    event_id,
                    "evidence.recorded",
                    source,
                    ref,
                    recorded - _seconds(rng, 1, 120),
                    recorded,
                    {"items": [_evidence_item(item) for item in items]},
                ),
                "evidence",
                len(evidence_events) + 1,
                index,
                (discovered,),
            )
            evidence_events.append(event_id)
        for decision in account.decisions:
            graph.add(
                _decision_envelope(plan, decision),
                "decision",
                decision.n,
                index,
                (discovered, *evidence_events),
            )
            if decision.persona is not None:
                graph.add(
                    _envelope(
                        decision.persona_id,
                        "persona.selected",
                        reference.sources["persona.selected"],
                        ref,
                        decision.persona_at,
                        decision.persona_at + _seconds(rng, 1, 30),
                        {
                            "persona": decision.persona,
                            "decision_event_id": decision.event_id,
                            "explanation": None,
                        },
                    ),
                    "persona",
                    decision.n,
                    index,
                    (decision.event_id,),
                )
            if decision.action_status is not None:
                graph.add(
                    _envelope(
                        decision.action_id,
                        "action.recorded",
                        reference.sources["action.recorded"],
                        ref,
                        decision.action_at,
                        decision.action_at + _seconds(rng, 1, 60),
                        {
                            "action_type": reference.action_type,
                            "play_id": decision.play,
                            "target_persona": decision.persona,
                            "status": decision.action_status,
                            "cost": decision.cost,
                            "currency": reference.currency,
                            "decision_event_id": decision.event_id,
                        },
                    ),
                    "action",
                    decision.n,
                    index,
                    (decision.event_id,),
                )
            observation = decision.observation
            if observation is not None:
                needs = [discovered, decision.event_id]
                if observation.shape in FROM_ACTION:
                    needs.append(decision.action_id)
                graph.add(
                    _outcome_envelope(plan, observation), "outcome", observation.n, index, needs
                )
    return graph.ordered()


def generate(config: DatasetConfig, *, artifacts: Mapping[str, LogicArtifact]) -> Schedule:
    """The complete seed schedule for `config`, as plain data."""
    plan = _plan(config, artifacts)
    stage_1 = _stage_one(plan)
    correction, new = _stage_two(plan)
    return build_schedule(
        canonical=plan.reference.envelopes,
        stage_1=stage_1,
        stage_2=(_outcome_envelope(plan, correction), _outcome_envelope(plan, new)),
        attribution_instant=config.attribution_instant,
    )
