# STATE.md — Current Repository State

> The only live status record. Keep it short and current. Not a backlog, roadmap, or branch log.

**Updated:** 2026-09-07

## Phase

**Phase 2 — Historical Decision Core: current; 2A and 2B complete, 2C next.** 2B built on `phase-2/evaluator` at `20e75ca`, reviewed CHANGES REQUIRED 2026-09-06 (two narrow gaps), corrected in `2558803` (task P2-B-02), accepted by the ChatGPT project reviewer 2026-09-07, merge authorized by the user, merged into `main` as `04222b3` (no-ff) on 2026-09-07. 2A accepted by ChatGPT/Codex re-review at `816b2db` on 2026-09-05, merge authorized by the user, merged into `main` as `a9a9cc4` (no-ff) on 2026-09-05. Phase 1 (Skeleton and One Trace) complete: accepted at `071da0a`, merged as `ffa42bd` on 2026-09-04. Phase 0 completed at `2cdb8b5`; stack ratified (D-010) at `d183f60`.

## Present Objective

Phase 2 in three narrow, sequential tasks, each reviewed before the next:

- **2A (done, merged `a9a9cc4`)** — domain projections (evidence versions, historical context, consumed inputs, logic artifacts, decisions, actions, outcomes) written atomically from accepted events; cross-event reference and time-boundary validation; database-level append-only protection for every projected historical record; the append-only evidence-correction/supersession representation. Closes full AC-15.
- **2B (done, merged `04222b3`)** — `evaluator-v1`: closed rule grammar over the schema-v1 artifacts (`logic/rules.py`), pure deterministic evaluation (`logic/evaluator.py`), exact reconstruction from the projection tables (`replay/reconstruct.py`) that verifies artifact presence, JSON, content hash, strict schema, row/content identity, decision/artifact identity, and runtime evaluator identity before evaluating; NovaSignal AI reconstructs to 86 / 75 / `PRIORITIZE` with the five stored contributions and the preserved `-v1` evidence ids (AC-01, INV-05); reconstruction writes nothing, reads no `events`/`accounts`, and is unchanged by later evidence, a second account, or a new artifact (AC-04, INV-01); every integrity failure is explicit and named (AC-07 integrity half, INV-09). Rule grammar closed with `fullmatch`; boundary awareness validated at `evaluate` entry.
- **2C** — correction immutability (AC-05, INV-04) and the before/at/after boundary tests (AC-03, INV-02).

Phase 2 is complete only when 2A, 2B, and 2C are all proven. Counterfactual replay under `v5.1` (AC-02) is Phase 3.

## Verified Repository Condition

- `main` = `04222b3` (= merge commit of `phase-2/evaluator` at `2558803` into `2577138`) plus this STATE commit; pushed; working tree clean. Local verification on `main` at `04222b3` (2026-09-07 Pacific): `uv run pytest` 262 passed (223 acceptance/unit, 39 invariant); `ruff check` and `ruff format --check` clean; `reset && seed` → 9 created, second `seed` → 0 created, 9 duplicate. CI on `main` at `04222b3`: run 34166348717, `lint` / `tests` / `invariants` green. Branch CI at `2558803`: run 34058929257 green.
- What exists: Python 3.13 project via uv; collector at `POST /api/v1/decision-events` with strict JSON-mode validation, canonical-JSON idempotency (200 duplicate / 409 conflict), atomic writes, account rules, six decision-envelope coherence validators, and cross-event reference and time-boundary validation at ingest (evidence `available_at` ≤ `decision_boundary`; persona/action at or after the decision boundary; outcome strictly after its action; artifact hash resolving to a registered artifact with matching identity; supersession target pre-existing). Schema-v1 event type `logic_artifact.registered` under the reserved `_system` principal (strict `LogicArtifact` model; excluded from every account-facing surface via `accounts_query()`; `/accounts/_system` → 404). SQLite ledger with `accounts`, append-only `events`, and eight append-only projection tables written atomically with each accepted event (`evidence_versions`, `logic_artifacts`, `decisions`, `decision_context`, `decision_consumed_inputs`, `persona_selections`, `actions`, `outcomes`), all guarded by database UPDATE/DELETE triggers. Evidence-version content identity = (`account_ref`, `evidence_type`, canonical `value_json`, `source`, `observed_at`, `available_at`, `supersedes_evidence_version_id`); exact re-mint → 422, different content → 409. Append-only evidence supersession link (`supersedes_evidence_version_id`). Canonical NovaSignal AI fixture: nine envelopes (two artifact registrations + seven account events) seeded only through the collector; the account trace still renders seven rows with the synthetic banner. `evaluator-v1` (`logic/rules.py` closed grammar over the schema-v1 artifacts, `logic/evaluator.py` pure deterministic evaluation, `replay/reconstruct.py` exact reconstruction from the projection tables with artifact presence, JSON, content hash, strict schema, row/content identity, decision/artifact identity, and runtime evaluator identity verified before evaluation; every failure explicit and named; reconstruction writes nothing and reads no `events`/`accounts` row). Full AC-15 closed; AC-01 closed (NovaSignal AI reconstructs to 86 / 75 / `PRIORITIZE` with the five stored contributions and the preserved `-v1` evidence ids); AC-04 closed for the reconstruction path; AC-07 integrity half closed; INV-01 enforced at the database on all projection tables and proven for reconstruction; INV-02 reference rule enforced at ingest with before/at/after coverage, evidence resolved by preserved id only, naive boundaries rejected on every path; INV-03 input states distinct (consumed / ignored / unavailable / absent); INV-04 representation proven; INV-05 verified before evaluation; INV-08 strict action-before-outcome; INV-09 no manufactured certainty in evaluation; INV-11 atomicity proven across events and every projection table.
- Not yet: decision inspection page, replay, attribution, Insights, 200-account dataset, README.

## Accepted Constraints Carried Forward (no `DECISIONS.md` entry; they enforce existing truth)

- Schema v1 keeps the closed seven-key typed evidence vocabulary through the MVP.
- `persona.selected` renders with kind label `EVENT`.
- A decision's `decision_boundary` is the decision event's occurrence instant; escalate before ever letting them differ.
- Counterfactual replay remains Phase 3.
- `evaluator-v1` temporal semantics (accepted by review 2026-09-06, reaffirmed 2026-09-07): "observed within N days before the decision boundary" reads the linked evidence version's `observed_at` as that calendar date at `00:00:00Z` and matches when it is not after `T(d)` and the elapsed time `T(d) − o` is at most `N × 24` hours (inclusive); future observations excluded; `PRODUCT.md` leaves this precision open, so it is evaluator-v1 semantics, not product truth, and Phase 3 replay under `v5.1` inherits it.
- Rule grammar is closed: a factor `rule` is exactly one of the four shapes present in the canonical artifacts, matched in full (`fullmatch`); anything else, including a trailing newline, is an explicit `UnsupportedRule`. `missing_value_behavior` other than `no_match_contributes_zero` is refused. Boolean values fit no rule shape.
- Reconstruction semantics: `consumed` = available and referenced by a factor whether or not the rule matched; comparison with the record is set equality of (`input_key`, `evidence_version_id`, `contribution`) over consumed factors; divergence raises `ReconstructionMismatch`, never a best-effort result; `evidence_versions` is read only by the preserved id.
- Reversible builder choices in force: SQLAlchemy Core; strictness applied at `validate_json(strict=True)`; `httpx` at runtime for the in-process seed; `_system` row is `name` "System (logic artifact registry)", `domain` `system.invalid`; `accounts_query()` lives in `ledger/schema.py`; `value_json` = evidence item minus its identity keys; outcome-ordering reason code `action_is_not_before_the_outcome` (ratified by review 2026-09-05); reconstruction lives in `replay/reconstruct.py` with the pure evaluator and grammar in `logic/`; `ArtifactMissing` is a sibling of `IntegrityFailure` under `ReconstructionError` (ratified by review 2026-09-06); `IntegrityFailure` carries `field`, `detail`, `stored`, `recomputed`; `context_states` is a read-only mapping; `require_aware_boundary` is public and applied at `evaluate` entry.

## Run

```bash
uv sync && uv run flight-recorder reset && uv run flight-recorder seed && uv run flight-recorder serve
```
Open `http://127.0.0.1:8000/accounts/novasignal-ai`. Tests: `uv run pytest`; invariants only: `uv run pytest -m invariant`.

## Blockers

None.

## Next Action (exactly one)

Scope Phase 2C narrowly (correction immutability: AC-05 / INV-04 full proof obligations; the before/at/after decision-boundary suite: AC-03 / INV-02) as `.handoffs/phase-2-task-2c.md`, send the task file for pre-dispatch review, then run it in Claude Code on a new branch `phase-2/corrections` from current `main`.
