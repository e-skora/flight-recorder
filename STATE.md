# STATE.md — Current Repository State

> The only live status record. Keep it short and current. Not a backlog, roadmap, or branch log.

**Updated:** 2026-09-04

## Phase

**Phase 1 — Skeleton and One Trace: implemented and corrected on branch `phase-1/skeleton-trace` at `071da0a`; refreshed review packet awaiting ChatGPT acceptance and the user's merge decision; not merged.** Phase 0 completed at `2cdb8b5`; stack ratified (D-010) at `d183f60`.

## Present Objective

Get `071da0a` accepted and merged into `main`, then scope Phase 2 (historical decision core) per the 2026-09-03 decision packet's direction.

## Verified Repository Condition

- `main` = `d183f60`: the four control files only. Remote `github.com/e-skora/flight-recorder`, in sync.
- Branch `phase-1/skeleton-trace` at `071da0a` (7 commits on top of `d183f60`, pushed, no PR). P1-02 (`602ba49`, `071da0a`) added six decision-envelope coherence validators: unavailable inputs carry no value and no evidence reference; unique `input_key`s in historical context and in consumed inputs; every consumed input matches exactly one available context entry with identical value (type-exact) and `evidence_version_id`; `decision_boundary` equals the decision event's `occurred_at` after UTC normalization. Base from P1-01: Python 3.13 project via uv; collector at `POST /api/v1/decision-events` with strict JSON-mode validation, canonical-JSON idempotency (200 duplicate / 409 conflict), atomic writes, account rules; SQLite ledger (`accounts`, append-only `events` with RAISE triggers, foreign keys on, deferred `first_seen_event_id` FK); canonical NovaSignal AI fixture (7 envelopes with stable `evidence_version_id`s, logic artifacts `v3.2` and `v5.1`, decision hash verified) seeded only through the collector; server-rendered `/` and `/accounts/{account_ref}` with text kind labels, separately labeled occurred/recorded times, synthetic banner in the shared layout.
- Tests on the branch: 74 passing (65 acceptance/unit, 9 invariant) locally and in CI run 33747333481 (`lint`, `tests`, `invariants` all green at `071da0a`; verified by the coordinator via `gh run view`). INV-01 and INV-11 enforced and exercised generatively. AC-15 Phase 1 subset proven; full AC-15 stays open until Phase 2 projections. Control files unchanged on the branch.
- Reversible in-task choices reported by the builder: SQLAlchemy Core; strictness applied at `validate_json(strict=True)` rather than model config; evidence items are a closed discriminated union of the seven canonical keys; `httpx` is a runtime dependency (seed goes through the in-process ASGI app); `persona.selected` renders with kind label `EVENT`.

## Run (on the branch)

```bash
uv sync && uv run flight-recorder reset && uv run flight-recorder seed && uv run flight-recorder serve
```
Open `http://127.0.0.1:8000/accounts/novasignal-ai`. Tests: `uv run pytest`; invariants only: `uv run pytest -m invariant`.

## Blockers

None. ChatGPT review of `a45042c` (2026-09-03) returned CHANGES REQUIRED; all ten of its criteria are implemented at `071da0a` and the refreshed packet recommends ACCEPT. Constraints confirmed by that packet: closed seven-key evidence vocabulary stays through the MVP; `persona.selected` stays `EVENT`; `decision_boundary` and the decision event's `occurred_at` are the same instant (escalate if a real need to differ appears); counterfactual replay stays Phase 3. Merge remains the user's decision after the refreshed packet.

## Next Action (exactly one)

Send the refreshed review packet (`.handoffs/review-packet-p1-01.md`, pinned to `071da0a`) to ChatGPT; on ACCEPT, the user decides the merge of `phase-1/skeleton-trace` into `main` (fast-forward), after which Phase 2 is scoped as one task.
