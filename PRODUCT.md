# GTM Flight Recorder: Enterprise Product Contract

> **Status:** Ratified repository product truth, adopted 2026-10-28 by D-028. It states required behavior and release obligations, not implemented capability; `STATE.md` says what is built today.  
> **Authority:** This file and `DECISIONS.md` are authoritative for durable product truth, subordinate only to the owner's explicit current decision. Changing the promise, the first-release boundary, a decision class, an invariant or a release gate requires the owner's explicit approval and a `DECISIONS.md` entry.  
> **First release:** the owner selected "Record and replay existing tools' decisions". Existing sales tools keep account scoring and routing; Flight Recorder records, verifies, investigates and supports honestly labeled replay. Vendor, hosting, database, authentication-provider and deployment choices are not yet decided.  
> **History:** the former synthetic-MVP contract is preserved at `docs/history/PRODUCT-synthetic-mvp.md`. References to `PRODUCT.md` sections in code comments, tests and decisions written before 2026-10-28 refer to that document.

## 1. Product and users

Flight Recorder is the decision observability and investigation platform for enterprise revenue operations. It connects a company's existing sales systems and workflows, preserves the recorded basis of automated decisions, makes failures and changes inspectable, and helps authorized people evaluate changes without rewriting history.

The primary operator is a GTM engineer, RevOps operator, or revenue-systems owner. Sales managers and authorized sales representatives are consumers of explanations and investigation results. Company administrators manage membership and connections. A company is a real authorization and data boundary, never a cosmetic label.

The first useful production workflow is: an operator connects an approved Salesforce organization and instruments one existing account-prioritization workflow; decisions arrive continuously with their historical context and logic identity; a sales manager questions an account's treatment; an authorized operator finds the decision, verifies its basis, compares a supported proposed rule version, and shares the investigation inside the company. A second operator can complete this without the founder's laptop or assistance.

A public synthetic demo remains separately deployed and cannot access production tenants or credentials. RelayBridge and NovaSignal AI remain fictional fixtures. Merge remains only a public reference archetype.

## 2. Scope and staged capability

### First production vertical slice

- Company workspaces, authenticated membership, permission enforcement, audit events, and service credentials.
- A versioned event-ingestion API and one supported production Salesforce read/observation connection.
- A documented way for an existing customer workflow to submit the evidence and logic/execution version it actually used. Polling current CRM fields alone cannot recover historical decision context.
- Account-prioritization inspection, search/filtering, trace navigation, saved investigation links, original verification where supported, and explicitly labeled counterfactual evaluation.
- Observable ingestion/connector health, backpressure and recovery, actionable errors, operational backup/restore, measured capacity, and support procedures.
- A real team's repeatable use and acceptance, not only a fictional-record developer-org walkthrough.

### Owner-selected first-release boundary

**Owner decision, 27 October 2026:** existing sales tools continue making and executing decisions; Flight Recorder records, verifies, investigates, and simulates. Optional managed scoring/routing would create an additional execution product with approval, rollout, rollback, conflict, and failure semantics. Do not quietly add it while implementing observability.

Account prioritization is the first fully supported class, not a permanent architectural ban on routing, qualification, or other decisions. The record model must name its decision class and replay capability. A second class enters only with its own semantics, workflow, and tests. Do not build a universal arbitrary-workflow executor before the first real team has a usable path.

### Separate later releases

HubSpot follows a working Salesforce/customer workflow. Clay/Apollo can initially submit through the supported collector contract; native connectors are added only when needed for a demonstrated customer workflow. CRM write-back and managed scoring/routing require their own qualified release. No autonomous outreach, causal-lift claim, compliance certification, or exact replay of an unretained external AI/model execution is implied.

## 3. End-to-end behavior

1. An administrator creates a company workspace and adds users. Invitations expire and cannot grant access to another company.
2. An authorized integration administrator connects the intended Salesforce organization through a supported, least-privilege flow. The product shows the organization and allowed operations without exposing credentials.
3. The operator selects the approved account/field scope and instruments the existing decision workflow. A connection test, metadata check, event receipt, decision verification, and continuously healthy ingestion are separate statuses.
4. Authorized ingestion authenticates a principal, derives company scope from that principal, validates the versioned envelope, and atomically admits or rejects the event. It never trusts a client-supplied company identifier as authorization.
5. The trace exposes evidence availability/consumption states, source observations, occurrence/availability/ingestion times, source execution and workflow versions, recorded output, downstream actions, and later outcomes.
6. The product either verifies a supported original, labels an original as recorded but not exactly verifiable, or exposes a named integrity/unavailability failure. It never substitutes today's CRM values for missing history.
7. An authorized operator selects a supported proposed logic version for counterfactual evaluation. Results are separate from originals, clearly labeled, and cannot trigger CRM writes or enter actual-outcome statistics.
8. The team can find and share an investigation through authenticated links, with filters and explanations understandable to its sales users. Access is checked on every read/export, not just on page navigation.
9. Connector failures, expired access, rate limiting, stale ingestion, and unavailable replay appear visibly with an actionable recovery path. Restarting a process does not silently lose or duplicate accepted work.

## 4. Data and replay contract

Preserve existing INV-01..INV-11, defined exactly in Appendix A with their acceptance mapping, and the legacy canonical fixtures/artifact hashes. Existing schema versions retain their exact wire semantics. New organization-scoped schemas and migrations must not add defaulted fields that silently change old canonical identities.

Production records add explicit company scope, actor/service-principal identity, source workflow/run identity, decision class, ingestion identity, provenance, and declared verification capability. A foreign company account ID, artifact hash, event ID, operation ID, or export reference cannot be used to read or mutate another company's record. Idempotency and uniqueness are scoped and tested deliberately; canonical content and authorization remain separate checks.

Replay capabilities are explicit:

- **Exactly verifiable deterministic execution:** retained context, declarative logic, evaluator identity, and required artifacts permit exact verification. Missing/mismatched artifacts fail visibly.
- **Recorded external execution:** retained inputs/outputs, prompt/model/tool/workflow identities and provenance can be inspected. Recording is not an exact reproduction guarantee.
- **Simulation or reevaluation:** newly executed results are labeled with execution time, version, and nondeterminism/availability limits. They never masquerade as historical events.

Production rules are organization-owned, versioned business logic, not the synthetic fixture weights. Historical scoring artifacts remain immutable; promotion of a new rule version is distinct from editing the old one. No rule editor is required before a supported artifact-registration/import workflow is usable and tested.

The system records business-outcome attribution under an explicit named/versioned policy. Unknown/open windows, unresolved attribution, heuristic inference, and counterfactual results remain separate. No observed association becomes a causal claim.

Retention and deletion must be designed before customer data is admitted: identify permitted data classes, retention periods, exports, and privileged deletion/restore authority. Append-only business records cannot be silently edited. If an approved retention/deletion action removes replay inputs, replay becomes explicitly unavailable and the governed deletion is auditable. The exact retention/immutability reconciliation needs an explicit design decision; no implementation may claim both indefinite reconstructability and complete deletion of the same data. Legacy canonical data is unaffected.

## 5. Enterprise access and safety obligations

- Authenticate humans and service principals; enforce company scope and role permissions on UI, API, searches, exports, jobs, caches, and connector operations.
- Proposed minimum roles: company administrator; integration administrator; operator; viewer. Privilege changes, revocation, and disabled accounts take effect consistently. Final role details are settled with the first deployment's workflow.
- Enterprise identity federation and mandatory MFA are release requirements when required for the target deployment. The first shared service may not invent a weak login as a temporary exception. Authentication protocol/provider selection receives a focused architecture decision.
- Credentials are isolated per connection/company, protected in storage and transit, redacted from logs and exports, and have tested rotation/revocation/expiration behavior. Existing local file permissions alone are not the hosted secret-management design.
- Ingestion has bounded request size, per-principal limits, timeouts, durable recovery, and explicit rejection. A noisy company cannot monopolize shared workers or expose another company's errors.
- Audit privileged access, membership/configuration changes, rule promotion, exports, and connector actions. Application-level integrity and externally anchored tamper evidence are different claims; the release must state its privileged-administrator threat boundary.
- Production data never enters the public demo, developer fixtures, AI services, or support exports by default. Any external data processing must be explicitly configured and authorized.

Use a versioned security verification baseline such as [OWASP ASVS 5.0.0](https://asvs.owasp.org/) to derive concrete tests. This is a proposed engineering baseline, not certification or a claim that every control is already met.

## 6. Operations and architecture obligations

The production service must run independently of the founder's Mac. The existing Python/FastAPI/domain/replay work is a starting point. Preserve and reuse it where suitable; no rewrite is required just to change branding or database.

Select a production persistence/deployment model by architecture decision against company isolation, concurrent writers, transaction semantics, migrations, recovery, and operating cost. A managed client/server database is the recommended candidate for a shared hosted service; no vendor or dependency is selected here. SQLite can remain for demo/local fixtures and migration verification. Storage choice alone does not establish tenant safety or enterprise readiness.

Required operations: versioned migrations with rollback/restore procedure; encrypted backups outside the serving failure domain; a demonstrated restore into an isolated environment; ingestion lag, queue/backlog, error and auth/connector health metrics; actionable alerts and an identified responder; bounded retries that distinguish definitely-unsent work from uncertain external writes; deploy/rollback procedures; incident and support procedures; dependency/security maintenance; documented data handling.

The following numbers are proposed initial engineering targets, **not measured capacity or customer SLAs**: one deployment supports 25 active team members, 100,000 account records, and 100,000 decisions/day at an explicitly modeled event-to-decision ratio. Exercise a declared 10-times-mean burst, concurrent UI traffic, and backfill while live ingestion runs. At that load, target p95 acknowledgment under one second and indexed account/decision lookup under two seconds, with zero accepted-event loss, no duplicate domain effects, and bounded backlog recovery. Data volume, payload sizes, retention, test duration, and error rate must be fixed in the workload before results count. The first customer's workload can change these proposed targets before implementation.

Proposed recovery targets for the first deployment: restored data loss no greater than 15 minutes and service restoration within four hours, demonstrated by a restore drill. Document actual measured results and cost before making a contractual promise. Public-demo regeneration is not this backup test.

## 7. Release gates

| Gate | Required evidence |
| --- | --- |
| Engineering baseline | Linux CI passes on the exact candidate; relevant macOS portability checks pass; known 11 failures are repaired without weakening tests; docs and current state agree with implemented behavior. |
| Private hosted integration | Authenticated roles and service keys; two-company adversarial isolation tests; approved secrets/data model; continuous Salesforce sandbox ingestion across expiry, pagination/limits, restart, duplicate input, outage, and recovery; deterministic replay still uses historical evidence. No private data in the public demo. |
| Real-team pilot | Explicitly authorized company/data scope, monitored deployment, restore drill, support owner, successful onboarding by someone other than the founder. At least two customer operators use a real recurring workflow over ten business days and document investigation usefulness, gaps, and acceptance. These are proposed acceptance criteria, not current achievements. |
| Enterprise production release | Agreed workload and recovery evidence; security review and target customer's required identity/access/procurement controls; access revocation/export/deletion behavior; migration/rollback drill; incident response and operating ownership; no unresolved high-severity security/data-integrity issue. Real-data activation and production deployment are separately authorized. |
| Optional CRM write release | Explicit customer/owner authority, smallest permission scope, per-target serialization, original-bound write intent, conditional/source-version handling, observed matching result, uncertain-send blocking, safe disable/rollback. A simulation or fake writer does not meet this gate. |

No stage may be called enterprise-ready because the synthetic demonstration passes, a UI renders, a connector logged in once, or a document was approved. A public demo and a production customer service have different acceptance criteria.

## 8. Product acceptance beyond infrastructure

A release must solve an actual sales-operations problem: the team can identify a problematic account decision, understand the recorded evidence and version that produced it, assess a supported change, and share the result with the right people. Instrumentation must fit a supported existing workflow and document gaps it cannot capture. The operator must see when data stopped arriving. Measure onboarding effort, investigation success/time, recurring use, and reported usefulness. Do not invent business lift or customer validation.

## 9. What does not change

Historical truth before convenience; evidence before explanation; no hindsight leakage; original/counterfactual separation; immutable identity and provenance; atomic collector admission/idempotency; honest outcome attribution; canonical synthetic fixtures kept as regression evidence; default-off external actions; least privilege; no production credentials or customer data in Git.

Existing synthetic fixtures remain regression assets, not production business policy. Presentation material becomes a byproduct of a useful, defensible product rather than the controlling completion gate.

## 10. Adoption and unresolved decisions

The enterprise destination and the observability-first release boundary are the owner's explicit decisions, recorded in D-028 on 2026-10-28. The initial deployment model, workload and recovery targets, retention semantics and a standing publication policy remain proposals; each is decided by the owner at its implementation gate and recorded in `DECISIONS.md`. Nothing in this contract by itself authorizes an implementation, account, credential, customer-data access, provider action, spending or deployment.

## Appendix A. Preserved semantic baseline and regression criteria

The following definitions and mappings are retained verbatim from PRODUCT.md at commit `585c58219f6afcb655be0af852536a9915a17fd7`. They remain authoritative for supported deterministic replay and the legacy synthetic regression suite. Their synthetic acceptance cases do not replace the production release gates above. AC-12 describes the synthetic demonstration; authenticated production records must instead carry their truthful source and capability labels.

### Core invariants

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

### Legacy acceptance mapping

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
