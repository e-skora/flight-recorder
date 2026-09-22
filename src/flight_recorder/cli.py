"""Console entry point.

`flight-recorder reset | seed | seed-dataset | register-current-logic |
attribute [--reevaluate] | serve`
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

from flight_recorder.ledger.database import db_path_from_env, reset_database


def cmd_reset(db_path: Path) -> int:
    reset_database(db_path)
    print(f"reset: schema created at {db_path}")
    return 0


def cmd_seed(db_path: Path) -> int:
    """Submit every canonical envelope through the collector on the in-process app."""
    import asyncio

    import httpx

    from flight_recorder.app import create_app
    from flight_recorder.fixtures import canonical_envelope_paths

    app = create_app(db_path)
    counts: Counter[str] = Counter()

    async def run() -> int:
        # The seed crosses the same HTTP boundary external workflows use,
        # served in-process over ASGI without a network socket.
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://seed") as client:
            for path in canonical_envelope_paths():
                response = await client.post(
                    "/api/v1/decision-events",
                    content=path.read_bytes(),
                    headers={"content-type": "application/json"},
                )
                body = response.json()
                status = body.get("status", "error")
                counts[status] += 1
                print(f"{response.status_code} {status:<9} {path.name}")
                if response.status_code >= 400:
                    print(f"seed: stopped at {path.name}: {body}", file=sys.stderr)
                    return 1
        print(
            f"seed: {counts['created']} created, {counts['duplicate']} duplicate"
            f" ({sum(counts.values())} envelopes)"
        )
        return 0

    return asyncio.run(run())


def cmd_register_current_logic(db_path: Path) -> int:
    """Submit the selected demo overlay's registration envelope (D-017).

    The same collector boundary `seed` uses, one envelope. It registers the
    successor artifact `v5.2` without touching the canonical nine: no existing
    artifact is edited, relabeled, deactivated or re-registered, and `v3.2` and
    `v5.1` keep their exact content and hashes.

    Idempotent by canonical-JSON event identity (INV-11): a second run creates
    nothing and appends no event.

    This command has no prerequisite of its own and registers happily on an
    empty database. What is *not* a supported setup order is registering the
    overlay before `seed-dataset`: the dataset's schedule requires the ledger
    to be a prefix of it, and an overlay event inside that prefix makes the
    next `seed-dataset` refuse with `ScheduleDiverged`. Run the dataset first.
    """
    import asyncio

    import httpx

    from flight_recorder.app import create_app
    from flight_recorder.fixtures import current_logic_registration_path

    app = create_app(db_path)
    counts: Counter[str] = Counter()
    path = current_logic_registration_path()

    async def run() -> int:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://register") as client:
            response = await client.post(
                "/api/v1/decision-events",
                content=path.read_bytes(),
                headers={"content-type": "application/json"},
            )
            body = response.json()
            status = body.get("status", "error")
            counts[status] += 1
            print(f"{response.status_code} {status:<9} {path.name}")
            if response.status_code >= 400:
                print(
                    f"register-current-logic: stopped at {path.name}: {body}",
                    file=sys.stderr,
                )
                return 1
        print(
            f"register-current-logic: {counts['created']} created, "
            f"{counts['duplicate']} duplicate ({sum(counts.values())} envelopes)"
        )
        return 0

    return asyncio.run(run())


def cmd_attribute(db_path: Path, reevaluate: bool = False) -> int:
    """Compute `outcome-attribution-v1` results and submit them through the collector.

    Without `--reevaluate`: initial attribution of every effective outcome
    version that has no result yet. With it: recompute outcomes that have a
    result and append a replacement only where the result changed.
    """
    from flight_recorder.app import create_app
    from flight_recorder.attribution.service import run_attribution

    run = run_attribution(create_app(db_path), reevaluate=reevaluate)
    for submission in run.submissions:
        print(
            f"{submission.http_status} {submission.status:<9} {submission.outcome_event_id}: "
            f"{submission.envelope['payload']['status']} "
            f"({submission.envelope['payload']['reason']})"
        )
        if submission.http_status >= 400:
            print(
                f"attribute: stopped at {submission.outcome_event_id}: {submission.body}",
                file=sys.stderr,
            )
    for outcome_event_id in run.unchanged:
        print(f"unchanged {outcome_event_id}")
    for outcome_event_id in run.already_attributed:
        print(f"skipped   {outcome_event_id}: already attributed")
    print(
        f"attribute{' --reevaluate' if reevaluate else ''}: cutoff {run.cutoff}; "
        f"{len(run.created)} created, {len(run.unchanged)} unchanged, "
        f"{len(run.already_attributed)} already attributed"
    )
    return 1 if run.failed else 0


def cmd_seed_dataset(db_path: Path) -> int:
    """Submit the synthetic dataset's seed schedule through the collector (D-014 Q4).

    The canonical nine, the generated stage-1 envelopes, one attribution
    operation per stage-1 outcome version at the scheduled cutoff, and the two
    stage-2 envelopes, which stay awaiting attribution. The digest's aggregates
    use the manifest's signal definitions and comparison workflow version,
    passed in explicitly.
    """
    from flight_recorder.app import create_app
    from flight_recorder.dataset.generator import generate
    from flight_recorder.dataset.schedule import ScheduleDiverged, SeedRefused, run_schedule
    from flight_recorder.fixtures import (
        canonical_artifacts,
        dataset_comparison_workflow_version,
        dataset_config,
        dataset_signals,
    )

    schedule = generate(dataset_config(), artifacts=canonical_artifacts())
    try:
        report = run_schedule(
            create_app(db_path),
            schedule,
            signals=dataset_signals(),
            comparison_workflow_version=dataset_comparison_workflow_version(),
        )
    except ScheduleDiverged as error:
        print(f"seed-dataset: {error}; nothing further was submitted", file=sys.stderr)
        return 1
    except SeedRefused as error:
        for result in error.accepted:
            print(f"{result.http_status} {result.status:<9} {result.item.event_id}")
        print(
            f"seed-dataset: stopped at item {error.item.sequence} ({error.item.event_id}): "
            f"{error.status} {error.body}",
            file=sys.stderr,
        )
        return 1
    for result in report.results:
        print(f"{result.http_status} {result.status:<9} {result.item.event_id}")
    print(
        f"seed-dataset: {report.created} created, {report.duplicate} duplicate "
        f"({len(report.results)} items); events {report.events_total} of "
        f"{report.scheduled_total} scheduled; digest {report.digest}"
    )
    if not report.fresh:
        print(
            "not a fresh seed: the ledger holds events outside the schedule (for example an "
            "independent attribute run); digest not claimed as the fresh-seed digest"
        )
    return 0


def cmd_serve(db_path: Path) -> int:
    import uvicorn

    from flight_recorder.app import create_app

    uvicorn.run(create_app(db_path), host="127.0.0.1", port=8000)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flight-recorder")
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite file (default: $FLIGHT_RECORDER_DB or ./flight_recorder.db)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("reset", help="delete the SQLite file and recreate the schema")
    sub.add_parser("seed", help="submit fixtures/canonical/ through the collector")
    sub.add_parser(
        "seed-dataset",
        help="submit the synthetic dataset's seed schedule through the collector",
    )
    sub.add_parser(
        "register-current-logic",
        help="submit fixtures/current/ through the collector; run after seed-dataset",
    )
    sub.add_parser("serve", help="run uvicorn on 127.0.0.1:8000")
    attribute = sub.add_parser(
        "attribute",
        help="attribute outcomes under outcome-attribution-v1 through the collector",
    )
    attribute.add_argument(
        "--reevaluate",
        action="store_true",
        help="recompute outcomes that already have a result; append only changed results",
    )
    args = parser.parse_args(argv)

    db_path = args.db if args.db is not None else db_path_from_env()
    if args.command == "attribute":
        return cmd_attribute(db_path, reevaluate=args.reevaluate)
    handlers = {
        "reset": cmd_reset,
        "seed": cmd_seed,
        "seed-dataset": cmd_seed_dataset,
        "register-current-logic": cmd_register_current_logic,
        "serve": cmd_serve,
    }
    return handlers[args.command](db_path)


if __name__ == "__main__":
    sys.exit(main())
