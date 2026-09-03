# STATE.md — Current Repository State

> The only live status record. Keep it short and current. Not a backlog, roadmap, or branch log.

**Updated:** 2026-09-03 (rev 2)

## Phase

**Phase 1 — Skeleton and One Trace: implemented on branch `phase-1/skeleton-trace` at `a45042c`; review returned CHANGES REQUIRED; correction task P1-02 pending; not merged.** Phase 0 completed at `2cdb8b5`; stack ratified (D-010) at `d183f60`.

## Present Objective

Close the decision-envelope coherence gap found in review (unavailable inputs with values, duplicate input keys, consumed inputs absent from or disagreeing with historical context, `decision_boundary` ≠ `occurred_at`), then return a refreshed commit-pinned review packet for the user's merge decision.

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

None. ChatGPT review of `a45042c` (2026-09-03): CHANGES REQUIRED — architecture, fixture, trace, ledger, idempotency, and disclosure accepted; the collector must reject internally contradictory `decision.recorded` envelopes before persistence. Constraints confirmed by the packet: closed seven-key evidence vocabulary stays through the MVP; `persona.selected` stays `EVENT`; `decision_boundary` and the decision event's `occurred_at` are the same instant (escalate if a real need to differ appears); counterfactual replay stays Phase 3. Merge remains the user's decision after the refreshed packet.

## Next Action (exactly one)

Run P1-02 (`.handoffs/phase-1-task-02.md`, local only) in Claude Code on `phase-1/skeleton-trace`: enforce the six decision-envelope coherence rules at validation with 422-and-no-write tests, keep every existing test green, push, CI green; then the coordinator refreshes the review packet at the new commit for the user's merge decision.
