# STATE.md — Current Repository State

> The only live status record. Keep it short and current. Not a backlog, roadmap, or branch log.

**Updated:** 2026-09-04

## Phase

**Phase 2 — Historical Decision Core: current; 2A implemented on `phase-2/projections` at `1dd52f5`; ChatGPT review returned CHANGES REQUIRED (two narrow semantics), corrections task P2-A-02 pending.** Phase 1 (Skeleton and One Trace) is complete: accepted by ChatGPT review at `071da0a`, merge authorized by the user, merged into `main` as `ffa42bd` on 2026-09-04. Phase 0 completed at `2cdb8b5`; stack ratified (D-010) at `d183f60`.

## Present Objective

Phase 2 in three narrow, sequential tasks, each reviewed before the next:

- **2A** — domain projections (evidence versions, historical context, consumed inputs, logic artifacts, decisions, actions, outcomes) written atomically from accepted events; cross-event reference and time-boundary validation; database-level append-only protection for every projected historical record; the append-only evidence-correction/supersession representation. Closes full AC-15.
- **2B** — `evaluator-v1` over the declarative artifact with artifact-hash and evaluator-identity verification; exact `v3.2` reconstruction of NovaSignal AI → score 86, `PRIORITIZE` (AC-01, INV-05); current-state isolation tests (AC-04, INV-01).
- **2C** — correction immutability (AC-05, INV-04) and the before/at/after boundary tests (AC-03, INV-02).

Phase 2 is complete only when 2A, 2B, and 2C are all proven. Counterfactual replay under `v5.1` (AC-02) is Phase 3.

## Verified Repository Condition

- `main` = `597bdff` plus this STATE commit (content identical to `ffa42bd` except `STATE.md`), pushed, working tree clean.
- `phase-2/projections` = `1dd52f5` (three commits on `597bdff`), pushed, not merged, no PR. Coordinator verification on that commit in a temporary worktree on the user's Mac (2026-09-04): `uv run pytest` 172 passed (136 acceptance/unit, 36 invariant); `ruff check` and `ruff format --check` clean; `reset && seed` → 9 created, second `seed` → 0 created, 9 duplicate; projection counts `evidence_versions` 7, `logic_artifacts` 2, `decisions` 1 (86 / 75 / `PRIORITIZE` / `v3.2` / `db3a8bde…`), `decision_context` 8, `decision_consumed_inputs` 5, `persona_selections` 1, `actions` 1, `outcomes` 1; `/` lists only NovaSignal AI; `/accounts/_system` → 404; `/accounts/novasignal-ai` still seven rows with the synthetic banner. Branch CI at `1dd52f5`: run 33907988381, `lint` / `tests` / `invariants` green. `PRODUCT.md`, `DECISIONS.md`, `AGENTS.md`, `STATE.md`, templates, CSS, and `web/summaries.py` untouched on the branch.
- What 2A adds (on the branch): schema-v1 event type `logic_artifact.registered` under the reserved `_system` principal (strict `LogicArtifact` model; excluded from every account-facing surface through `accounts_query()` in `ledger/schema.py`); eight append-only projection tables written atomically with each accepted event (`evidence_versions`, `logic_artifacts`, `decisions`, `decision_context`, `decision_consumed_inputs`, `persona_selections`, `actions`, `outcomes`), each guarded by database UPDATE/DELETE triggers; cross-event reference and time-boundary validation at ingest; the append-only evidence supersession representation. Canonical seed is nine envelopes; the NovaSignal AI trace is still seven rows. Full AC-15 closed on the branch; INV-01 enforced at the database on all projection tables; INV-02 reference rule enforced at ingest with before/at/after coverage; INV-04 representation proven; INV-05 identity bound to artifact hash plus evaluator version.
- What exists on `main` (Phase 1): Python 3.13 project via uv; collector at `POST /api/v1/decision-events` with strict JSON-mode validation, canonical-JSON idempotency (200 duplicate / 409 conflict), atomic writes, account rules, and six decision-envelope coherence validators; SQLite ledger with `accounts` and append-only `events`; canonical NovaSignal AI fixture seeded only through the collector; server-rendered `/` and `/accounts/{account_ref}` trace.
- Not yet: evaluator, exact `v3.2` reconstruction, decision inspection page, replay, attribution, Insights, 200-account dataset, README.

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

Run P2-A-02 (`.handoffs/phase-2-task-2a-02.md`, local only) in Claude Code on `phase-2/projections` from `1dd52f5`: add `available_at` to evidence-version content identity (same id, different availability → 409) and require outcomes strictly after their action. Then the coordinator verifies, refreshes the review packet, and the user sends it for re-review; merge only on ACCEPT and user authorization.
