# GTM Flight Recorder — Product Truth

> **Status:** Ratified repository product truth  
> **Provenance:** Distilled from `02-MVP-CONTRACT.md` v1.2 and `05-CORE-INVARIANTS.md` v1.1, both ratified by explicit user decision on 2026-09-01, plus the principles from `04-PRODUCT-PRINCIPLES.md` needed to preserve their meaning. Initialized 2026-09-02.  
> **Authority:** This file and `DECISIONS.md` are now authoritative for durable product truth, subordinate only to the user's explicit current decision. Where this file is silent, the ratified sources remain informative; where it is explicit, it controls.  
> **Change control:** Changing a MUST, WILL NOT, the primary user, core promise, decision class, an invariant, a canonical score, or a completion gate requires the user's explicit approval and a `DECISIONS.md` entry. Editorial clarification that does not alter behavior or proof obligations may be made directly. Conflicts are surfaced, never silently reconciled.

Contract language: **MUST** = required for the relevant completion gate; **MUST NOT** = prohibited; **SHOULD** = expected unless a documented reason justifies omission; **MAY** = permitted; **WILL NOT** = outside the MVP.

## 1. What This Is

**Category:** Observability for automated revenue decisions.

**Thesis:** Once GTM software starts making decisions rather than merely executing instructions, revenue teams need a way to inspect, replay, and debug those decisions.

**Product form:** A web application with a small, versioned decision-event ingestion API. It is not fundamentally an MCP server; an MCP or agent-query interface is a possible post-MVP extension and is not required.

**Primary user:** A GTM engineer, technical RevOps operator, or revenue-systems owner responsible for automated account-prioritization logic at a scaling B2B SaaS company.

**Organizational ICP:** A lean technical GTM team, thousands of accounts, material signal-based automation, a fragmented revenue stack, frequently changing logic, and outcomes evaluable within useful windows. Roughly 75–500 employees is the design center, not an eligibility rule.

**Roles that MUST stay distinct:**

| Role | Definition |
| --- | --- |
| Organizational ICP | The category above, not one named company |
| Primary user | GTM engineer, technical RevOps operator, or revenue-systems owner |
| Verified reference archetype | **Merge** — used only as public evidence that the operating pattern exists |
| Demo operating company | **RelayBridge** — explicitly fictional B2B integration and agent-connectivity platform |
| Ledger account | A fictional prospect RelayBridge evaluates; canonical prospect **NovaSignal AI** |

The product and portfolio materials MUST NOT present Merge as a customer or tenant, claim Merge lacks internal observability, invent private Merge workflows or outcomes, or brand the application as built for Merge.

**The problem:** The user's stack can discover, enrich, score, route, and contact accounts, but mainly preserves current fields and completed actions. When a prioritization decision looks wrong later, the user cannot reliably recover what evidence was available, which evidence the logic consumed, which logic version ran, why, what action followed, what outcome occurred, or whether current logic would decide differently from the same historical evidence. Current data and edited workflows create hindsight contamination.

## 2. Core Promise and Proof Obligations

**Core promise:** For an account-prioritization decision submitted through the documented collector boundary, GTM Flight Recorder MUST preserve and expose the historical decision context, reproduce the original deterministic result, compare it with the result produced by current logic using only that historical context, and connect the decision to later action and outcome records under an explicit attribution policy.

The MVP MUST prove all five claims:

1. **Instrumentability** — a real workflow could submit a versioned decision event through a documented, idempotent collector contract.
2. **Traceability** — an operator can follow an account from discovery through outcome and inspect the basis of its important automated decision.
3. **Historical integrity** — later data, corrections, outcomes, and logic changes do not rewrite what the system knew or decided earlier.
4. **Counterfactual replay** — current logic can evaluate the preserved historical context without importing present-day evidence.
5. **System learning** — aggregated decision records recover known descriptive patterns planted in the synthetic dataset.

The MVP does not need to prove production scale, integration breadth, or autonomous execution.

**One-sentence MVP:** A deterministic, portfolio-grade application in which a GTM engineer can ingest and inspect one automated account-prioritization decision end to end, reproduce it from preserved historical context and immutable declarative logic, compare that same context under current logic without hindsight leakage, connect the decision to later outcomes through an explicit attribution policy, and recover honest planted patterns from synthetic accounts.

## 3. MVP Boundary

**Fully supported, replayable decision class (exactly one):** Account prioritization — `PRIORITIZE` or `DO_NOT_PRIORITIZE`. This decision MUST expose its historical context, consumed inputs, contributions, score, logic version, confidence when meaningful, resulting action, cost, outcome, and replay comparison.

**Surrounding trace events (required, not independently replayable):** discovery, enrichment, persona selection, outbound action, and outcome. They MAY carry explanations and versions; persona selection, play selection, and outreach generation are not required to be replayable. Additional replayable decision classes are post-MVP.

**Explicit non-goals — the MVP WILL NOT include:** production Apollo, Clay, Salesforce, HubSpot, or sales-engagement integrations; a complete CRM or system of record; autonomous prospecting or outreach execution; email generation or sequence management; production multi-tenant auth, permissions, billing, or compliance architecture; enterprise-scale ingestion or streaming; an MCP server or agent-query interface; a generalized platform for every GTM decision class; exact replay of arbitrary nondeterministic external models; causal-inference claims from synthetic observational data; a full no-code scoring-model editor; required LLM calls in the core decision or replay path; human-versus-AI performance analysis; automated experimentation; production confidence calibration; integrations built only to display vendor logos; or infrastructure designed for hypothetical scale. Post-MVP ideas belong in a parking lot, not the active build.

## 4. Required Product Experiences

These are required experiences, not necessarily separate routes or screens.

### 4.1 Decision-Event Collector

One versioned ingestion boundary: `POST /api/v1/decision-events`.

The documented envelope MUST include `schema_version`, a globally stable `event_id`, `event_type`, `source`, a stable `account_ref`, timezone-aware `occurred_at` and `recorded_at`, and a typed `payload`.

The collector MUST support the event types needed for the canonical trace: account discovery, evidence recording, decision recording, persona-selection recording, action recording, and outcome recording. A decision payload MUST carry or reference its decision boundary, preserved historical context, consumed inputs, immutable logic artifact, and result. A resulting action MUST be recorded as a distinct action event linked to its decision.

Idempotency: repeating the same `event_id` with a semantically identical envelope MUST be idempotent, where semantic identity means equality after schema validation and canonical JSON serialization (key order and insignificant formatting do not matter; every submitted field does). Reusing an `event_id` with different canonical content MUST fail visibly. Invalid events MUST be rejected without partial domain writes.

At least one documented JSON example MUST show how a Clay HTTP step, n8n workflow, or equivalent could submit a decision. The canonical demo seed MUST enter through this collector boundary; tests MAY call the underlying service directly.

### 4.2 Account Selection

The user MUST be able to locate and open NovaSignal AI inside the RelayBridge operating-company context. This is presentation context, not multi-tenancy. The product SHOULD expose enough additional synthetic accounts to make the dataset and Insights credible.

### 4.3 Account Trace

The user MUST see the account's material events in chronological order: account discovered → enrichment recorded → evidence available → prioritization decision made → persona selected → outbound action taken → outcome evaluated. The trace MUST distinguish events, evidence, decisions, actions, and outcomes rather than presenting them as interchangeable records.

### 4.4 Decision Inspection

Opening the prioritization decision MUST show: decision timestamp; decision type and output; numeric score and threshold; workflow version; historical logic version; immutable logic-artifact identity; declarative ruleset and evaluator version; historical evidence context; which inputs the historical logic consumed; per-input contribution where supported; evidence source and relevant time metadata; confidence only if meaningfully produced; a human-readable explanation clearly distinguished from evidence; downstream action and cost; and the later outcome with its evaluation window and attribution status.

The UI MUST make unavailable, ignored, and consumed inputs distinguishable.

### 4.5 Decision Replay

The user MUST be able to compare:

```text
preserved historical context + historical logic = original decision
preserved historical context + current logic    = counterfactual current decision
```

The comparison MUST show historical and current logic versions; original and counterfactual scores; original and counterfactual outputs; changed, added, removed, or reweighted contributions; and any input required by current logic that was unavailable historically. The product MUST NOT imply that the counterfactual decision actually occurred.

### 4.6 Aggregate Insights

At least one aggregate view across the synthetic dataset MUST show: a signal or rule; the number of decisions in which it was available, consumed, or matched, with those states clearly distinguished; relevant outcome counts or rates; an observed comparison; attribution coverage or unresolved-outcome count; and labeling sufficient to prevent correlation being read as causation. Insights MUST display sample size and SHOULD expose the applicable outcome window. Counterfactual results MUST be excluded from aggregates of actual decisions and outcomes unless shown in a separately labeled simulation view.

### 4.7 Synthetic-Data Disclosure

The product and README MUST state plainly that Apollo-, Clay-, CRM-, and outcome-like events are simulated. The interface MUST NOT suggest connection to live customer systems. The interface and README MUST identify RelayBridge, NovaSignal AI, all other ledger accounts, and their decisions and outcomes as fictional or synthetic. Merge MAY be cited only as a public reference archetype, with an explicit statement that this establishes neither a customer relationship nor an unmet need.

## 5. Required Historical Semantics

Notation (from the invariants): `d` is a recorded prioritization decision; `T(d)` its explicit decision-time boundary; `H(d)` its preserved historical context (values and availability states presented to the decision system); `U(d)` the subset of `H(d)` consumed by historical logic; `Lh(d)` the preserved historical declarative logic artifact plus evaluator identity; `Lc` an explicitly selected current artifact plus evaluator identity; `R(L, H)` deterministic evaluation. The only two valid replay questions are `R(Lh(d), H(d))` = original decision and `R(Lc, H(d))` = counterfactual. Neither permits current evidence to replace or augment `H(d)`.

- **Three things stay distinct:** historical context, consumed inputs, and current account state. Current logic MAY use a historical signal the earlier logic ignored; later evidence MUST NOT enter replay.
- **Decision-time boundary:** each decision has an explicit boundary. Evidence not in `H(d)` at that boundary is missing or unknown during replay; the implementation MUST NOT fetch or substitute its current value. When evidence and decision share a displayed timestamp, inclusion is determined by recorded transaction/order or sealed-context membership, never by display precision.
- **Corrections append:** a correction creates a new evidence version or event and MUST NOT mutate the version preserved for an earlier decision. It MAY affect later decisions.
- **Historical logic is data:** a label such as `v3.2` is insufficient. Scoring logic MUST be stored as immutable declarative data preserving artifact and schema versions, decision class, factor definitions and weights, missing-value behavior, threshold and output mapping, activation metadata, canonical content hash, and the evaluator-code version required to interpret it. Changing any of these creates a new immutable identity. The artifact hash and evaluator identity MUST be verified before exact replay; a missing or mismatched artifact MUST fail explicitly, never fall back to a best-effort reconstruction presented as exact.
- **Explanations are derived:** an explanation MUST be derived from or linked to recorded inputs and logic and MUST NOT replace them. A persisted original explanation and its linkage MUST remain; regeneration creates a new derived version or an ephemeral presentation and MUST NOT overwrite the original while presenting itself as historical. LLM-generated narrative MUST be labeled explanation, not evidence; the deterministic core MUST remain inspectable without an LLM.
- **Confidence is honest:** confidence MAY be stored only when it has a defined meaning. The canonical decision does not emit confidence; the UI MUST show that it was not provided rather than inventing a percentage.
- **Outcomes are later, separate observations:** recorded separately from the decision and MUST NOT rewrite it. Each evaluated outcome has an explicit type, occurrence/observation time, and evaluation window; a still-open window MUST NOT be treated as failure. Corrections to outcomes or attribution append or explicitly supersede; they never rewrite in place.
- **Outcome attribution policy (`outcome-attribution-v1`):** (1) use an explicit, valid source-provided action or decision reference; (2) otherwise attribute to the most recent eligible outbound action for the same account that occurred before the outcome and within the declared window; (3) otherwise leave the outcome unresolved. An eligible action is an actual, recorded outbound action linked to an account-prioritization decision for the same account, with sent or completed status, occurring before the outcome and no more than 90 days earlier; counterfactual and failed-to-execute actions are ineligible. An explicit reference is valid only when it resolves to the original prioritization decision and/or its eligible action for the same account; if both are supplied their recorded relationship MUST agree. Every attribution result records policy version, method, window, referenced action and decision when resolved, attribution time, and status `direct`, `inferred`, or `unresolved`. Unresolved outcomes remain visible and MUST NOT enter decision-level performance metrics as if resolved; inferred attribution MUST be labeled heuristic; actual outcomes are never attributed to counterfactual decisions or actions.
- **Replay honesty:** the MVP's deterministic logic MUST reproduce the original decision exactly. Documentation MUST NOT generalize that guarantee to nondeterministic models or unavailable external services; a replay lacking required execution artifacts is marked unavailable or non-exact, never fabricated.

## 6. Required Conceptual Records

The implementation MAY name or organize records differently but MUST represent these concepts and relationships:

- **Account** — stable identifier; name and domain; basic firmographic display fields.
- **Event** — stable identifier; ingestion schema version; account; event type; source; occurrence time; recorded time; payload or reference.
- **Evidence Version** — stable immutable identifier; account; type and value; provenance/source; observed or effective time when meaningful; recorded/available time; source confidence when meaningful; correction/supersession relationship when applicable.
- **Historical Decision Context** — decision; candidate input key; preserved value or unavailable status; linked evidence version when available.
- **Consumed Decision Input** — decision; input key; preserved value; linked evidence version when applicable; contribution or role.
- **Logic Version** — stable identifier; decision class; immutable declarative rules, weights, missing-value behavior, threshold, output mapping; artifact schema version and evaluator-code version; artifact hash or equivalent integrity identifier; activation metadata.
- **Decision** — stable identifier; account; decision class; decision timestamp/boundary; workflow version; logic version; output; score and threshold when applicable; confidence when defined; explanation reference.
- **Action** — stable identifier; triggering decision; action type; time; status; synthetic cost.
- **Outcome** — stable identifier; account and any unmodified explicit source-provided action/decision reference; type and value; occurred/observed time; recorded time; evaluation window.
- **Outcome Attribution** — stable identifier; outcome; policy version and method; attribution window; action and decision reference when resolved; attribution time; status `direct` / `inferred` / `unresolved`.

A mutable foreign-key relationship alone is not a historical snapshot.

## 7. Canonical Demo Fixture

The canonical scenario MUST use **RelayBridge** (explicitly fictional demo operating company whose product helps B2B software teams connect their products and AI agents to customers' business systems through unified APIs and agent tooling) evaluating **NovaSignal AI** (explicitly fictional prospect). These roles and facts MUST be identical across collector payloads, UI, tests, screenshots, and README, sourced from one canonical location. Merge MUST NOT appear as a tenant or ledger account.

**Historical context at decision time (all sealed as available by the boundary):**

| Fact | Value |
| --- | --- |
| Decision boundary | `2026-04-17T10:05:02Z` |
| Employees | `184` |
| Industry | `B2B AI Software` |
| Headquarters | `United States` |
| Funding | Series B announced `18 days` earlier |
| Hiring | `7` open platform-engineering positions |
| Executive change | Head of Platform joined `43 days` earlier |
| Verified integration pressure | `LOW` — one documented production integration, no public API, no agent-tool directory at the boundary |
| Workflow version | `v4.2` |

Evidence recorded after the boundary is excluded even if it describes an earlier real-world event. The integration-pressure evidence MUST be present in the historical context but ignored by `v3.2` — demonstrating the difference between available and consumed evidence.

**Historical logic `v3.2`:** employee range match +25; B2B AI software vertical +20; recently funded +18; platform-engineering hiring +15; US headquarters +8 → score **86**, threshold **75**, decision **`PRIORITIZE`**.

**Current logic `v5.1`:** employee range match +25; B2B AI software vertical +20; recently funded +4; platform-engineering hiring +15; US headquarters +8; low integration pressure −21 → score **51**, threshold **75**, decision **`DO_NOT_PRIORITIZE`**.

**Action and outcome:** enroll in synthetic outbound play `#14` targeting Head of Platform; recorded synthetic cost `$1.42`; outcome window `90 days`; attribution `direct` to play `#14` under `outcome-attribution-v1`; reply no, meeting no, opportunity no. The demo MAY describe the prioritization hypothesis as unsupported within the window; it MUST NOT claim the funding signal caused the negative outcome. The canonical decision emits no confidence value.

**Canonical demo journey the build MUST support:**

1. Open NovaSignal AI inside RelayBridge and see the end-to-end trace.
2. Inspect the original prioritization decision and its score of 86.
3. See the historical evidence, provenance, consumed inputs, and model `v3.2`.
4. See the downstream action, cost, and negative 90-day outcome.
5. Open Insights and observe that the synthetic funding signal has little descriptive relationship with opportunity creation, verified integration pressure has a stronger relationship, and workflow `v4.2` underperforms its comparison cohort.
6. Replay the preserved historical context under `v5.1`, which uses the historically available integration-pressure evidence.
7. See the score change 86 → 51 and the output change `PRIORITIZE` → `DO_NOT_PRIORITIZE`.
8. Verify that no present-day evidence entered the comparison.

An in-product rule editor is not required; both logic versions MAY be predefined fixtures.

## 8. Synthetic Dataset Requirements

A deterministic, seeded synthetic dataset with at least **200** accounts and enough decision/outcome variation to support Insights. It MUST use plausible B2B firmographic and GTM values, preserve temporal ordering, contain positive and negative outcomes, include logic and workflow versions, include evidence-source metadata, produce the same canonical results from the same seed, and avoid real personal data.

The generator MUST plant three documented synthetic behaviors:

1. `recently_funded` appears in at least 50 prioritization decisions with an absolute observed 90-day opportunity-rate difference of no more than 2 percentage points between decisions with and without it;
2. `verified_integration_pressure = HIGH` appears in at least 40 decisions with an observed 90-day opportunity rate at least 10 percentage points higher than the comparison group;
3. workflow `v4.2` appears in at least 40 decisions with an observed 90-day opportunity rate at least 8 percentage points below the documented comparison workflow cohort.

Expected effects MUST be stored in a machine-readable manifest separate from the analytics implementation. Analytics tests MUST recover all three from generated events within tolerance; the UI MUST NOT hard-code the conclusions. These planted behaviors validate the demonstration and calculations only — they are not market findings or causal evidence. The generator MAY be code- or fixture-driven. The final demo MUST NOT depend on a live third-party API or paid credential.

## 9. Acceptance Criteria

| ID | Criterion |
| --- | --- |
| AC-01 | Preserved NovaSignal AI context + logic `v3.2` → score 86, `PRIORITIZE`. |
| AC-02 | Same context + logic `v5.1` → score 51, `DO_NOT_PRIORITIZE`. |
| AC-03 | Evidence available one millisecond after the boundary changes neither the original reconstruction nor a counterfactual run. |
| AC-04 | Changing current employee count, industry, funding status, or hiring data changes neither the preserved context nor replay results. |
| AC-05 | Correcting a historical evidence value creates a new version/event; the earlier decision still references the original value. |
| AC-06 | An input current logic expects but the historical context lacks is marked missing/unknown; current data is never substituted. |
| AC-07 | Changing a logic label without changing artifact identity does not silently alter results; an unavailable or integrity-failing artifact makes exact replay fail explicitly. |
| AC-08 | Every evidence value consumed by the decision exposes a source and the time metadata showing when it became available. |
| AC-09 | Adding, hiding, or regenerating explanation text changes no evidence, logic, score, or decision; a persisted original explanation is never overwritten. |
| AC-10 | Appending or explicitly superseding an outcome changes neither the decision record nor historical replay. |
| AC-11 | Insights show sample size and descriptive language; never causal lift, never counterfactuals in actual statistics, never an available-but-ignored signal counted as consumed. |
| AC-12 | A viewer can determine from both product and README that Apollo-, Clay-, CRM-, and outcome-like data is synthetic. |
| AC-13 | Same seed, context, and logic version → same fixtures, scores, outputs, and aggregates across runs. |
| AC-14 | The canonical dataset reproduces all three planted effects from the same seed; Insights calculations derive from ingested events, not display text. |
| AC-15 | A valid canonical event via `POST /api/v1/decision-events` creates the expected record; canonically identical retry (even with reordered keys/formatting) creates no duplicate; conflicting reuse or invalid envelope fails visibly without partial writes. |
| AC-16 | The same outcome fixtures produce `direct`, `inferred`, and `unresolved` cases; unresolved outcomes stay out of decision-level metrics; no actual outcome attaches to a counterfactual decision. |
| AC-17 | Changing any rule, weight, threshold, missing-value behavior, artifact schema, or evaluator version creates or requires a new immutable logic identity; historical replay keeps the preserved combination. |
| AC-18 | The automated suite includes generative or property-based cases injecting post-decision evidence, mutating current state, and varying timestamps immediately before, at, and after the boundary; the invariant suite passes in CI before ledger or replay work is complete. |
| AC-19 | (SHOULD) A first-time viewer understands within 30 seconds that the product records why automated GTM decisions occurred and compares historical vs current logic without hindsight leakage. |

## 10. Core Invariants

Non-negotiable semantic properties. They formalize the contract and grant no permission to expand scope. Weakening, removing, or excepting one requires the user's explicit approval and a `DECISIONS.md` entry.

| ID | Rule | Covers |
| --- | --- | --- |
| INV-01 Historical Record Immutability | Once `d` is recorded, its timestamp, preserved context, consumed inputs, logic identity, output, score, and originally persisted explanation linkage MUST NOT be mutated by later account updates, corrections, logic changes, actions, outcomes, or regenerated explanations. Mutation tests must change current data, append corrected evidence, add an outcome, and activate new logic while the original stays equivalent in every protected field. | AC-04, AC-05, AC-10 |
| INV-02 Time-Bounded Historical Context | `H(d)` contains only values and availability states preserved at `T(d)`; `available_at(e) > T(d) ⇒ e ∉ H(d)`. Boundary tests cover immediately before, exactly at, and immediately after, with timezone-aware timestamps. | AC-03, AC-04 |
| INV-03 Historical Input States Remain Distinct | Distinguish: available and consumed; available but ignored; explicitly unavailable at `T(d)`; absent from the preserved context. Current logic MAY use ignored-but-available evidence; it treats absent/unavailable inputs as unknown. The canonical fixture shows integration pressure available, ignored by `v3.2`, consumed by `v5.1`; a separate test shows a historically unavailable input staying unknown; aggregate tests keep availability, consumption, and matching distinct. | AC-02, AC-06, AC-11 |
| INV-04 Evidence Version and Provenance Integrity | Every consumed value references an immutable evidence version with provenance and availability-time metadata. Tests append a corrected version, retain both, and prove the original decision resolves to the original value and provenance. | AC-05, AC-08 |
| INV-05 Declarative Logic and Evaluator Integrity | Every exactly replayable decision preserves immutable declarative rules, weights, missing-value behavior, threshold, output mapping, artifact schema, and evaluator-code identity. A label is metadata, not identity. Hash and evaluator identity are verified before exact replay; a missing or mismatched artifact is an explicit integrity failure. | AC-01, AC-07, AC-13, AC-17 |
| INV-06 Original and Counterfactual Cannot Be Blurred | Stored original `R(Lh(d), H(d))` and computed counterfactual `R(Lc, H(d))` have separate labels, records, and persistence; the counterfactual never overwrites the original or appears as an event that occurred. | AC-01, AC-02, AC-10 |
| INV-07 Explanation Is Not Evidence or Logic | Explanation presents the recorded basis; it is never the authoritative evidence, logic, or score. Regeneration is a new derived version or ephemeral; the original linkage stays intact; generated statements are traceable to recorded inputs or labeled interpretation. | AC-09 |
| INV-08 Outcomes Remain Later, Separate Observations | Outcomes have explicit type and window; decision-level credit comes only from the named, versioned attribution policy as direct/inferred/unresolved; corrections append or supersede. The UI distinguishes an open window, a negative evaluated outcome, missing outcome data, and heuristic attribution. | AC-10, AC-16 |
| INV-09 Unknown and Uncertainty Remain Honest | Preserve unknown/unavailable state; never manufacture certainty; confidence appears only with a defined source and meaning. Tests exercise missing-input and missing-artifact paths; the UI renders explicit unknown, unavailable, or integrity-failure states. | AC-06, AC-07 |
| INV-10 Demonstration Claims Are Truthful | Synthetic data, simulated vendor events, observational comparisons, public reference-company evidence, and counterfactual outputs are labeled accurately. Never imply live integration, a real customer, private knowledge, causal proof, or an action that did not occur. | AC-11, AC-12, AC-14, AC-16 |
| INV-11 Collector Identity and Idempotency | Every accepted event crosses the versioned collector with a stable identity; canonically identical retry has one domain effect; conflicting reuse or invalid envelope fails visibly without partial writes; canonical demo data uses the same boundary documented for external workflows. Tests cover valid ingestion, reordered-key retry, conflicting reuse, malformed payload, unsupported schema version, and atomic failure. | AC-15 |

**Invariant review gate:** a feature touching evidence, time, decisions, replay, explanations, actions, outcomes, or Insights is complete only when the affected invariants are named; relevant automated tests (including property-based cases where required) pass locally and in CI; failure behavior is visible rather than silently repaired; completion evidence is pinned to the implemented commit; and any accepted semantic change is recorded durably in the repository. Passing ordinary UI or happy-path tests does not satisfy this gate.

## 11. Technical and Operational Constraints

The MVP MUST: run locally from documented setup steps; use deterministic logic for the replayed decision class; require no production credentials for the canonical demo; include automated tests for AC-01 through AC-18 where technically applicable; run the invariant suite in CI; preserve timestamps with unambiguous timezone handling; use one canonical source for shared demo constants; fail visibly when replay integrity cannot be established; avoid color as the only distinction between original, counterfactual, consumed, ignored, missing, or failed-integrity states; and keep the core path understandable to another engineer.

The implementation SHOULD: minimize dependencies and moving parts; prefer a vertical slice over premature platform architecture; make invariant tests easy to locate; keep the collector schema small, versioned, and documented with at least one external-workflow example; support a fresh seeded reset; remain keyboard-usable through the canonical demo path with readable labels and contrast; and keep derived aggregate data reproducible from source fixtures.

The implementation MAY use any reasonable web stack and relational database. Stack selection MUST NOT weaken the historical semantics above. Language, framework, database engine, test runner, CI provider, and repository layout are intentionally open; see `DECISIONS.md`.

## 12. Completion Gates

**Build-complete** only when: all required experiences work with the canonical dataset; AC-01 through AC-18 pass or have explicit, user-approved exceptions; RelayBridge and NovaSignal AI fixture roles and facts are consistent everywhere; the required dataset and Insights view exist; a fresh local run is documented and reproducible; repository operating files are current; and no known defect violates a core invariant.

**Portfolio-ready** only when the above holds and the artifact additionally includes: a README explaining the problem, thesis, architecture, replay semantics, synthetic-data boundary, setup, and important tradeoffs; the collector schema and at least one example workflow payload; clear screenshots of the trace and replay comparison; a polished 60–90 second demo path, recorded or ready to record; a concise architecture diagram; evidence that invariant tests pass, including visible CI evidence; and public language that does not overstate integrations, causality, production readiness, or autonomous capability. A deployed demo MAY be added if it improves accessibility without disproportionate build time.

Public materials MUST describe the category broadly as observability for automated revenue decisions and the MVP precisely as fully replaying the account-prioritization decision class.

## 13. Decision Principles

Apply in order when several options satisfy the contract; an earlier principle outweighs a later one unless the user decides otherwise.

1. **Preserve historical truth before optimizing convenience** — reject any shortcut that lets an old decision change when present-day data, evidence, or logic changes.
2. **Evidence before explanation** — build the inspectable input and logic view before improving generated prose.
3. **Observability before more automation** — prefer recording a simulated action over building a real executor.
4. **Time is part of the data** — reject schemas or interfaces that collapse observed, available, decision, action, correction, and outcome times into one timestamp.
5. **Build the smallest complete proof** — deepen the prioritization path before adding another decision type, integration, or dashboard.
6. **Deterministic core, AI at the interpretive edge** — add an LLM only where it produces visible value without weakening replay integrity.
7. **GTM realism over vendor theater** — coherent synthetic data over shallow live integrations for logos.
8. **Claims no stronger than the evidence** — show counts, rates, windows, caveats; never relabel association as causal lift.
9. **The demo story is a product test** — the NovaSignal AI journey is the shortest proof that the concepts fit; keep its facts canonical everywhere.
10. **Optimize portfolio signal per unit of complexity** — a small, legible implementation of a hard semantic problem over enterprise scaffolding.

**Decision filter for optional work:** Does it preserve the contract and every invariant? Does it make the central user problem easier to understand or solve? Does it strengthen the canonical demo or expose meaningful engineering judgment? Is it the smallest credible way to achieve that value? Can its claim be supported honestly? Classify as **required now**, **useful next**, **parked**, or **rejected**. When uncertain, default to the smaller reversible choice and preserve the open question.
