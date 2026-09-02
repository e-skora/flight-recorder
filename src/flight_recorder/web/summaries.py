"""One-line trace summaries and text kind labels derived from typed payloads."""

import json
from dataclasses import dataclass

KIND_LABELS = {
    "account.discovered": "EVENT",
    "evidence.recorded": "EVIDENCE",
    "decision.recorded": "DECISION",
    "persona.selected": "EVENT",
    "action.recorded": "ACTION",
    "outcome.evaluated": "OUTCOME",
}


@dataclass(frozen=True)
class TraceRow:
    ingest_sequence: int
    event_id: str
    event_type: str
    kind: str
    source: str
    occurred_at: str
    recorded_at: str
    summary: str


def _yes_no(flag: bool, word: str) -> str:
    return word if flag else f"no {word}"


def summarize(event_type: str, payload: dict) -> str:
    match event_type:
        case "account.discovered":
            return f"Account discovered: {payload['name']} ({payload['domain']})"
        case "evidence.recorded":
            parts = []
            for item in payload["items"]:
                value = item["value"]
                parts.append(f"{item['evidence_type']} = {value}")
            return "Evidence recorded: " + "; ".join(parts)
        case "decision.recorded":
            result = payload["result"]
            logic = payload["logic_artifact"]
            return (
                f"Prioritization decision: {result['output']}, "
                f"score {result['score']} / threshold {result['threshold']}, "
                f"logic {logic['logic_version']}"
            )
        case "persona.selected":
            return f"Persona selected: {payload['persona']}"
        case "action.recorded":
            return (
                f"Outbound play #{payload['play_id']} to {payload['target_persona']}, "
                f"cost ${payload['cost']}, {payload['status']}"
            )
        case "outcome.evaluated":
            return (
                f"Outcome after {payload['window_days']} days: "
                f"{_yes_no(payload['reply'], 'reply')}, "
                f"{_yes_no(payload['meeting'], 'meeting')}, "
                f"{_yes_no(payload['opportunity'], 'opportunity')}"
            )
    return event_type


def trace_row(row) -> TraceRow:
    payload = json.loads(row.payload)
    return TraceRow(
        ingest_sequence=row.ingest_sequence,
        event_id=row.event_id,
        event_type=row.event_type,
        kind=KIND_LABELS[row.event_type],
        source=row.source,
        occurred_at=row.occurred_at,
        recorded_at=row.recorded_at,
        summary=summarize(row.event_type, payload),
    )
