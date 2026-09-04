"""Console entry point: `flight-recorder reset | seed | serve`."""

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
    args = parser.parse_args(argv)

    db_path = args.db if args.db is not None else db_path_from_env()
    handlers = {"reset": cmd_reset, "seed": cmd_seed, "serve": cmd_serve}
    return handlers[args.command](db_path)


if __name__ == "__main__":
    sys.exit(main())
