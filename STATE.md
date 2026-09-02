# STATE.md — Current Repository State

> The only live status record. Keep it short and current. Not a backlog, roadmap, or branch log.

**Updated:** 2026-09-02

## Phase

**Phase 1 — Skeleton and One Trace: stack ratified, implementation not started.** Phase 0 (truth layer) completed at commit `2cdb8b5`.

## Present Objective

Deliver the smallest runnable application and test harness that seeds the canonical NovaSignal AI fixture through `POST /api/v1/decision-events` and renders its chronological discovery-to-outcome trace inside the RelayBridge context.

## Verified Repository Condition

- Remote: `https://github.com/e-skora/flight-recorder` (public), default branch `main`, in sync with local `main`.
- Contents: the four control files only. No application code, tests, README, CI, dependencies, or `.gitignore` yet.
- Stack, tooling, and layout are decided (D-010): Python 3.13, FastAPI + Jinja2, SQLite via SQLAlchemy 2.0 with the UTC `Z` text-timestamp convention, pytest + Hypothesis, ruff, GitHub Actions, uv, `src/flight_recorder/` layout with `fixtures/canonical/` as the single source of demo constants.

## What Was Established Since Phase 0

- `DECISIONS.md` D-010 records the implementation stack; the Open section now holds only the deployed-demo and LLM-explanation questions plus reversible in-task details.

## Blockers

None.

## Next Action (exactly one)

Write the first scoped Claude Code task for Phase 1: project scaffold per D-010 (`pyproject.toml`, `uv.lock`, `.python-version`, `.gitignore`, `ci.yml`, `src/` and `tests/` skeleton), the canonical NovaSignal AI fixture as collector envelopes in `fixtures/canonical/`, a collector that validates and stores them idempotently, and one server-rendered trace page — with tests for AC-15 and the trace ordering. Exit: a first-time viewer can see the account path end to end.
