# STATE.md — Current Repository State

> The only live status record. Keep it short and current. Not a backlog, roadmap, or branch log.

**Updated:** 2026-09-06

## Phase

**Phase 2 — Historical Decision Core: current; 2A complete, 2B corrected, awaiting re-review.** 2B built on `phase-2/evaluator` at `20e75ca` (from `main` `4ede1b3`) on 2026-09-05; reviewed 2026-09-06 CHANGES REQUIRED on two narrow gaps (rule patterns accepted a trailing newline; the pure evaluator did not validate boundary awareness at entry); corrected in `2558803` (task P2-B-02) and coordinator-verified 2026-09-06; not merged. 2A accepted by ChatGPT/Codex re-review at `816b2db` on 2026-09-05, merge authorized by the user, merged into `main` as `a9a9cc4` (no-ff) on 2026-09-05. Phase 1 (Skeleton and One Trace) complete: accepted at `071da0a`, merged as `ffa42bd` on 2026-09-04. Phase 0 completed at `2cdb8b5`; stack ratified (D-010) at `d183f60`.

## Present Objective

Phase 2 in three narrow, sequential tasks, each reviewed before the next:

- **2A (done, merged `a9a9cc4`)** — domain projections (evidence versions, historical context, consumed inputs, logic artifacts, decisions, actions, outcomes) written atomically from accepted events; cross-event reference and time-boundary validation; database-level append-only protection for every projected historical record; the append-only evidence-correction/supersession representation. Closes full AC-15.
- **2B (corrected at `2558803` on branch `phase-2/evaluator`, awaiting re-review)** — `evaluator-v1`: closed rule grammar over the schema-v1 artifacts (`logic/rules.py`), pure deterministic evaluation (`logic/evaluator.py`), exact reconstruction from the projection tables (`replay/reconstruct.py`) that verifies artifact presence, JSON, content hash, strict schema, row/content identity, decision/artifact identity, and runtime evaluator identity before evaluating; NovaSignal AI reconstructs to 86 / 75 / `PRIORITIZE` with the five stored contributions and the preserved `-v1` evidence ids (AC-01, INV-05); reconstruction writes nothing, reads no `events`/`accounts`, and is unchanged by later evidence, a second account, or a new artifact (AC-04, INV-01); every integrity failure is explicit and named (AC-07 integrity half, INV-09). Branch verification at `2558803`: 262 tests (223 + 39 invariant; rule grammar closed with `fullmatch`, boundary awareness validated at `evaluate` entry), ruff clean, seed 9/0 then 0/9, CI run 34058929257 green.
- **2C** — correction immutability (AC-05, INV-04) and the before/at/after boundary tests (AC-03, INV-02).

Phase 2 is complete only when 2A, 2B, and 2C are all proven. Counterfactual replay under `v5.1` (AC-02) is Phase 3.

## Verified Repository Condition

- `main` = `b6987fc` (= merge commit `a9a9cc4` of `phase-2/projections` at `816b2db`, plus one housekeeping commit adding `Claude outputs/` to `.gitignore`) plus this STATE commit; pushed; working tree clean. Local verification on `main` at `b6987fc` (2026-09-05 Pacific): `uv run pytest` 173 passed (137 acceptance/unit, 36 invariant); `ruff check` and `ruff format --check` clean; `reset && seed` → 9 created, second `seed` → 0 created, 9 duplicate. CI on `main` at `b6987fc`: run 34006172832, `lint` / `tests` / `invariants` green. Branch CI at `816b2db`: run 34003913282 green.
- What exists: Python 3.13 project via uv; collector at `POST /api/v1/decision-events` with strict JSON-mode validation, canonical-JSON idempotency (200 duplicate / 409 conflict), atomic writes, account rules, six decision-envelope coherence validators, and cross-event reference and time-boundary validation at ingest (evidence `available_at` ≤ `decision_boundary`; persona/action at or after the decision boundary; outcome strictly after its action; artifact hash resolving to a registered artifact with matching identity; supersession target pre-existing). Schema-v1 event type `logic_artifact.registered` under the reserved `_system` principal (strict `LogicArtifact` model; excluded from every account-facing surface via `accounts_query()`; `/accounts/_system` → 404). SQLite ledger with `accounts`, append-only `events`, and eight append-only projection tables written atomically with each accepted event (`evidence_versions`, `logic_artifacts`, `decisions`, `decision_context`, `decision_consumed_inputs`, `persona_selections`, `actions`, `outcomes`), all guarded by database UPDATE/DELETE triggers. Evidence-version content identity = (`account_ref`, `evidence_type`, canonical `value_json`, `source`, `observed_at`, `available_at`, `supersedes_evidence_version_id`); exact re-mint → 422, different content → 409. Append-only evidence supersession link (`supersedes_evidence_version_id`). Canonical NovaSignal AI fixture: nine envelopes (two artifact registrations + seven account events) seeded only through the collector; the account trace still renders seven rows with the synthetic banner. Full AC-15 closed; INV-01 enforced at the database on all projection tables; INV-02 reference rule enforced at ingest with before/at/after coverage; INV-04 representation proven; INV-05 identity bound to artifact hash plus evaluator version; INV-08 strict action-before-outcome; INV-11 atomicity proven across events and every projection table.
- On branch `phase-2/evaluator` (not on `main` until accepted and merged): the evaluator and exact `v3.2` reconstruction above. Not yet: decision inspection page, replay, attribution, Insights, 200-account dataset, README.

## Accepted Constraints Carried Forward (no `DECISIONS.md` entry; they enforce existing truth)

- Schema v1 keeps the closed seven-key typed evidence vocabulary through the MVP.
- `persona.selected` renders with kind label `EVENT`.
- A decision's `decision_boundary` is the decision event's occurrence instant; escalate before ever letting them differ.
- Counterfactual replay remains Phase 3.
- Reversible builder choices in force: SQLAlchemy Core; strictness applied at `validate_json(strict=True)`; `httpx` at runtime for the in-process seed; `_system` row is `name` "System (logic artifact registry)", `domain` `system.invalid`; `accounts_query()` lives in `ledger/schema.py`; `value_json` = evidence item minus its identity keys; outcome-ordering reason code `action_is_not_before_the_outcome` (ratified by review 2026-09-05).

## Run

```bash
uv sync && uv run flight-recorder reset && uv run flight-recorder seed && uv run flight-recorder serve
```
Open `http://127.0.0.1:8000/accounts/novasignal-ai`. Tests: `uv run pytest`; invariants only: `uv run pytest -m invariant`.

## Blockers

None.

## Next Action (exactly one)

Send the refreshed `.handoffs/review-packet-p2b.md` for re-review at `2558803`; on ACCEPT and the user's explicit authorization, merge `phase-2/evaluator` into `main` (`--no-ff`), re-verify on `main`, record the 2B accepted constraints here; on CHANGES REQUIRED, write the `-03` correction task.
