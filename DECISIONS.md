# GTM Flight Recorder — Decisions

> **Purpose:** Durable record of material product and architecture decisions, with rationale and provenance.  
> **Rule:** Record only decisions actually established by the user, the ratified sources, or a later user-ratified decision packet. Do not invent decisions to fill gaps; unresolved choices stay in the "Open" section until decided. Superseding a decision appends a new entry that references the old one; entries are never rewritten in place.  
> **Sources referenced:** `02` = `02-MVP-CONTRACT.md` v1.2 (ratified 2026-09-01); `05` = `05-CORE-INVARIANTS.md` v1.1 (ratified 2026-09-01); `04` = `04-PRODUCT-PRINCIPLES.md`; `03` = `03-DUAL-MODEL-BUILD-PLAYBOOK.md` (process only).

## Ratified Decisions

### D-001 — Product form: web application with a versioned decision-event ingestion API

- **Decision:** The MVP is a web application plus a small collector API at `POST /api/v1/decision-events`. It is not fundamentally an MCP server; an MCP or agent-query interface is a possible post-MVP extension and is an explicit MVP non-goal.
- **Why:** The proof is that a real workflow could instrument a decision and an operator could inspect and replay it. An MCP surface would add a second interface without strengthening that proof.
- **Provenance:** `02` §2 (Product Form), §5.1, §11. Status: ratified 2026-09-01.

### D-002 — Primary user and organizational ICP

- **Decision:** The primary user is a GTM engineer, technical RevOps operator, or revenue-systems owner responsible for automated account-prioritization logic at a scaling B2B SaaS company. The organizational ICP is the category (lean technical GTM team, thousands of accounts, signal-based automation, fragmented stack, changing logic, evaluable outcomes; ~75–500 employees as design center), not a named company.
- **Why:** A narrow, technical primary user makes the semantics (provenance, versioning, replay) the product rather than dashboard polish, and keeps the ICP from collapsing into one reference company.
- **Provenance:** `02` §2. Status: ratified 2026-09-01.

### D-003 — Exactly one fully replayable decision class: account prioritization

- **Decision:** The MVP fully supports replay for account prioritization (`PRIORITIZE` / `DO_NOT_PRIORITIZE`) only. Discovery, enrichment, persona selection, outbound action, and outcome events are required in the trace but are not independently replayable. Additional replayable classes are post-MVP.
- **Why:** One decision implemented completely demonstrates more engineering and product judgment than several implemented superficially.
- **Provenance:** `02` §4; `04` principle 5. Status: ratified 2026-09-01.

### D-004 — Canonical fixture: RelayBridge evaluates NovaSignal AI; Merge is a public reference archetype only

- **Decision:** The fictional demo operating company is **RelayBridge** (B2B integration and agent-connectivity platform). The canonical fictional prospect is **NovaSignal AI**. **Merge** is cited only as public evidence that the operating archetype exists and MUST NOT appear as a customer, tenant, or ledger account, nor be described as lacking internal observability. The canonical facts (boundary `2026-04-17T10:05:02Z`, employees 184, industry B2B AI Software, US HQ, Series B 18 days prior, 7 open platform-engineering roles, Head of Platform joined 43 days prior, verified integration pressure LOW, workflow `v4.2`; `v3.2` → 86 / `PRIORITIZE`; `v5.1` → 51 / `DO_NOT_PRIORITIZE`; threshold 75; play `#14`, cost `$1.42`, 90-day window, direct attribution, no reply/meeting/opportunity; no confidence emitted) are fixed in `PRODUCT.md` §7 and MUST come from one canonical source in the codebase.
- **Why:** A consistent fictional company avoids overclaiming a customer relationship while keeping the demo grounded in a verified real-world pattern. Fixing the numbers makes AC-01/AC-02 exact and keeps UI, tests, screenshots, and README aligned.
- **Supersedes:** The Acme Robotics / Industrial Automation / VP Sales / 82%-confidence example used in `01-GTM-FLIGHT-RECORDER-SPEC.md`, `03` §4 and §10, `04` principle 9, and `06-PORTFOLIO-OBJECTIVE.md` §5 and §11. Those references are lower-authority illustrations and are superseded wherever they conflict.
- **Provenance:** `02` §2 (roles table), §5.2, §5.7, §8; `05` INV-03 and INV-10. Status: ratified 2026-09-01.

### D-005 — Deterministic core; scoring logic stored as immutable declarative data with hash and evaluator identity

- **Decision:** The replayed decision class uses deterministic logic. Scoring logic is stored as immutable declarative artifacts (factors, weights, missing-value behavior, threshold, output mapping, schema version, activation metadata, canonical content hash, evaluator-code version). Changing any of these yields a new identity. Exact replay verifies hash and evaluator identity first and fails explicitly otherwise. No LLM call is required in the core decision or replay path; any LLM output is labeled explanation, never evidence.
- **Why:** Exact reproduction of a historical decision is the product's central claim; a version label pointing at mutable code cannot support it, and a nondeterministic core cannot be tested against the acceptance criteria.
- **Provenance:** `02` §6.4, §6.5, §6.9, §11, §12; `05` INV-05, INV-07; `04` principle 6. Status: ratified 2026-09-01.

### D-006 — Collector contract: versioned envelope, canonical-JSON idempotency, atomic failure, seed enters through the collector

- **Decision:** One ingestion boundary, `POST /api/v1/decision-events`, with envelope fields `schema_version`, `event_id`, `event_type`, `source`, `account_ref`, `occurred_at`, `recorded_at`, `payload`. Idempotency is defined by equality after schema validation and canonical JSON serialization; conflicting reuse of an `event_id` and invalid envelopes fail visibly without partial writes. The canonical demo seed enters through this boundary (tests may call the underlying service directly). At least one documented example shows a Clay HTTP step, n8n workflow, or equivalent submitting a decision.
- **Why:** Instrumentability is one of the five proof claims; if the demo data bypassed the collector, the boundary would be untested theater.
- **Provenance:** `02` §5.1, AC-15; `05` INV-11. Status: ratified 2026-09-01.

### D-007 — Outcome attribution policy `outcome-attribution-v1`

- **Decision:** Decision-level credit uses a named, versioned policy: (1) valid explicit source-provided action/decision reference; (2) otherwise the most recent eligible outbound action for the same account before the outcome and within 90 days; (3) otherwise unresolved. Eligible actions are actual, recorded, sent/completed outbound actions linked to a prioritization decision for the same account; counterfactual and failed-to-execute actions are ineligible. Results record policy version, method, window, references, time, and status `direct` / `inferred` / `unresolved`. Unresolved outcomes stay visible and out of decision-level metrics; inferred attribution is labeled heuristic.
- **Why:** Silently crediting every account outcome to every preceding decision would make the Insights view dishonest and blur original and counterfactual results.
- **Provenance:** `02` §6.7, §6.8, AC-16; `05` INV-08. Status: ratified 2026-09-01.

### D-008 — Synthetic data only; seeded deterministic dataset with three planted, manifest-declared effects

- **Decision:** All Apollo-, Clay-, CRM-, and outcome-like events are simulated and disclosed as such in product and README. No live third-party API or paid credential is used. The dataset is deterministic and seeded, with at least 200 accounts, and plants exactly three documented effects (neutral `recently_funded`; positive `verified_integration_pressure = HIGH`; `v4.2` workflow underperformance) whose expected values live in a machine-readable manifest separate from the analytics code. Analytics must recover them from ingested events; the UI must not hard-code the conclusions.
- **Why:** Synthetic integrations prove the architecture without spending the MVP on credentials and plumbing, and planted effects make the Insights calculations verifiable rather than decorative — provided they are labeled as demonstration data, not market findings.
- **Provenance:** `02` §5.7, §9, AC-12, AC-13, AC-14; `05` INV-10; `04` principles 7 and 8. Status: ratified 2026-09-01.

### D-009 — Repository truth layer and authority after distillation

- **Decision:** From 2026-09-02 the repository and Git are authoritative for durable product truth, decisions, implementation state, and continuity. The truth layer is `PRODUCT.md` (product truth), `DECISIONS.md` (this file), `AGENTS.md` (coding-agent operating rules), and `STATE.md` (current phase and next action). The eight ChatGPT Project source files are not copied into the repository. Phase 0 deliberately creates no README, application or test scaffolding, architecture/data-model/demo-scenario/build-ledger docs, archived source copies, review-packet copy, or planning artifacts; those arrive only when real work justifies them.
- **Why:** A fresh session must recover what the product means and what to do next from repository evidence, not conversation. Empty scaffolding is not continuity, and duplicated sources create competing versions of reality.
- **Provenance:** `02` header (authority clause) and §14; `03` §4–§5 and §13 (process); user's explicit Phase 0 instruction, 2026-09-02, which narrows `03`'s suggested initial layout (README, `docs/`, `app/`, `tests/`) to the four control files for this initialization. Status: ratified by user instruction 2026-09-02.

## Open (Intentionally Unresolved)

These are not decisions. The ratified sources leave them open; nothing here may be assumed by an implementer without a recorded decision. Per the responsibility boundaries, a high-lock-in choice is proposed with tradeoffs and ratified by the user; ordinary reversible choices inside an approved task do not need an entry.

- **Implementation language and web framework.** `02` §12 permits any reasonable web stack; `01` §11 lists Next.js/React and Python or TypeScript as non-binding candidates.
- **Database engine.** `02` §12 permits any reasonable relational database; `01` §11 mentions PostgreSQL with SQLite as a fast-prototype option, non-binding.
- **Test framework and property-based testing library** (required by AC-18; tool unchosen).
- **CI provider** (required by AC-18 and the portfolio gate; provider unchosen).
- **Repository layout** (`app/` vs `src/`, test location) beyond the four control files.
- **Whether a deployed demo is built** (`02` §13: MAY, only if it improves accessibility without disproportionate build time).
- **Whether any LLM-generated explanation is included at all** (`02` §6.5 permits it at the interpretive edge; not required).
