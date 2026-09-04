# STATE.md — Current Repository State

> The only live status record. Keep it short and current. Not a backlog, roadmap, or branch log.

**Updated:** 2026-09-04

## Phase

**Phase 2 — Historical Decision Core: current, not started.** Phase 1 (Skeleton and One Trace) is complete: accepted by ChatGPT review at `071da0a`, merge authorized by the user, merged into `main` as `ffa42bd` on 2026-09-04. Phase 0 completed at `2cdb8b5`; stack ratified (D-010) at `d183f60`.

## Present Objective

Phase 2 in three narrow, sequential tasks, each reviewed before the next:

- **2A** — domain projections (evidence versions, historical context, consumed inputs, logic artifacts, decisions, actions, outcomes) written atomically from accepted events; cross-event reference and time-boundary validation; database-level append-only protection for every projected historical record; the append-only evidence-correction/supersession representation. Closes full AC-15.
- **2B** — `evaluator-v1` over the declarative artifact with artifact-hash and evaluator-identity verification; exact `v3.2` reconstruction of NovaSignal AI → score 86, `PRIORITIZE` (AC-01, INV-05); current-state isolation tests (AC-04, INV-01).
- **2C** — correction immutability (AC-05, INV-04) and the before/at/after boundary tests (AC-03, INV-02).

Phase 2 is complete only when 2A, 2B, and 2C are all proven. Counterfactual replay under `v5.1` (AC-02) is Phase 3.

## Verified Repository Condition

- `main` = `ffa42bd` (merge of `phase-1/skeleton-trace` at `071da0a`; content identical to the branch except `STATE.md`), pushed, working tree clean. Local verification on the merge commit: `uv run pytest` 74 passed (65 acceptance/unit, 9 invariant); `ruff check` and `ruff format --check` clean; `reset && seed` → 7 created, second `seed` → 0 created, 7 duplicate. Branch CI at `071da0a`: run 33747333481, `lint` / `tests` / `invariants` green.
- What exists: Python 3.13 project via uv; collector at `POST /api/v1/decision-events` with strict JSON-mode validation, canonical-JSON idempotency (200 duplicate / 409 conflict), atomic writes, account rules, and six decision-envelope coherence validators; SQLite ledger with `accounts` and append-only `events` (RAISE triggers, foreign keys on, `ingest_sequence` tie-break); canonical NovaSignal AI fixture (seven envelopes, stable `evidence_version_id`s, logic artifacts `v3.2` and `v5.1`, decision hash verified) seeded only through the collector; server-rendered `/` and `/accounts/{account_ref}` trace with text kind labels, separately labeled occurred/recorded times, synthetic banner in the shared layout.
- Not yet: normalized domain projections (full AC-15 open), evaluator, decision inspection page, replay, attribution, Insights, 200-account dataset, README.

## Accepted Constraints Carried Forward (no `DECISIONS.md` entry; they enforce existing truth)

- Schema v1 keeps the closed seven-key typed evidence vocabulary through the MVP.
- `persona.selected` renders with kind label `EVENT`.
- A decision's `decision_boundary` is the decision event's occurrence instant; escalate before ever letting them differ.
- Counterfactual replay remains Phase 3.
- Reversible builder choices in force: SQLAlchemy Core; strictness applied at `validate_json(strict=True)`; `httpx` at runtime for the in-process seed.

## Run

```bash
uv sync && uv run flight-recorder reset && uv run flight-recorder seed && uv run flight-recorder serve
```
Open `http://127.0.0.1:8000/accounts/novasignal-ai`. Tests: `uv run pytest`; invariants only: `uv run pytest -m invariant`.

## Blockers

None. The 2A design question is resolved (2026-09-04, reviewer recommendation adopted by the user): logic artifacts enter through the collector as schema-v1 event type `logic_artifact.registered` under a reserved `_system` principal excluded from all account-facing surfaces; embedding the artifact in each decision is rejected. The 2A task incorporates the five review corrections (system principal foreign-key handling, current base SHA, strict typed artifact validation, evidence serialization/equality contract, within-envelope evidence-id uniqueness).

## Next Action (exactly one)

Run Phase 2A (`.handoffs/phase-2-task-2a.md`, local only) in Claude Code on a new branch `phase-2/projections` from current `main`; the coordinator reconciles its report and prepares a review packet before 2B is scoped.
