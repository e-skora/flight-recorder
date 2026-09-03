# STATE.md — Current Repository State

> The only live status record. Keep it short and current. Not a backlog, roadmap, or branch log.

**Updated:** 2026-09-03

## Phase

**Phase 1 — Skeleton and One Trace: implemented on branch `phase-1/skeleton-trace` at `a45042c`, awaiting review; not merged.** Phase 0 completed at `2cdb8b5`; stack ratified (D-010) at `d183f60`.

## Present Objective

Get the first complete NovaSignal AI trace reviewed (ChatGPT review packet, then user merge decision) before Phase 2 (historical decision core) is scoped.

## Verified Repository Condition

- `main` = `d183f60`: the four control files only. Remote `github.com/e-skora/flight-recorder`, in sync.
- Branch `phase-1/skeleton-trace` at `a45042c` (5 commits on top of `d183f60`, pushed, no PR): Python 3.13 project via uv; collector at `POST /api/v1/decision-events` with strict JSON-mode validation, canonical-JSON idempotency (200 duplicate / 409 conflict), atomic writes, account rules; SQLite ledger (`accounts`, append-only `events` with RAISE triggers, foreign keys on, deferred `first_seen_event_id` FK); canonical NovaSignal AI fixture (7 envelopes with stable `evidence_version_id`s, logic artifacts `v3.2` and `v5.1`, decision hash verified) seeded only through the collector; server-rendered `/` and `/accounts/{account_ref}` with text kind labels, separately labeled occurred/recorded times, synthetic banner in the shared layout.
- Tests on the branch: 49 passing (40 acceptance/unit, 9 invariant) locally and in CI run 33696972847 (`lint`, `tests`, `invariants` all green at `a45042c`). INV-01 and INV-11 enforced and exercised generatively. AC-15 Phase 1 subset proven; full AC-15 stays open until Phase 2 projections. Control files unchanged on the branch.
- Reversible in-task choices reported by the builder: SQLAlchemy Core; strictness applied at `validate_json(strict=True)` rather than model config; evidence items are a closed discriminated union of the seven canonical keys; `httpx` is a runtime dependency (seed goes through the in-process ASGI app); `persona.selected` renders with kind label `EVENT`.

## Run (on the branch)

```bash
uv sync && uv run flight-recorder reset && uv run flight-recorder seed && uv run flight-recorder serve
```
Open `http://127.0.0.1:8000/accounts/novasignal-ai`. Tests: `uv run pytest`; invariants only: `uv run pytest -m invariant`.

## Blockers

None. Merge of `phase-1/skeleton-trace` into `main` is a user decision pending review.

## Next Action (exactly one)

Send the P1-01 review packet (`.handoffs/review-packet-p1-01.md`, local only) to ChatGPT for the first-trace review; on its disposition, the user decides the merge, and Phase 2 (evidence versions, decision projections, logic evaluator, original reconstruction for AC-01) is scoped.
