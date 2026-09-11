"""Console entry point: `flight-recorder reset | seed | attribute [--reevaluate] | serve`."""

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
    handlers = {"reset": cmd_reset, "seed": cmd_seed, "serve": cmd_serve}
    return handlers[args.command](db_path)


if __name__ == "__main__":
    sys.exit(main())
