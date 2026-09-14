# AGENTS.md — Operating Rules for Coding Agents

This repository builds **GTM Flight Recorder**, observability for automated revenue decisions. These rules apply to every coding agent (Claude Code, Codex, or any other) working here.

## 1. Start Here

Read, in order: this file → `PRODUCT.md` → `STATE.md` → `DECISIONS.md` (when the task touches a prior material choice) → the relevant code and tests. Do not reread the whole repository or produce a status ceremony each session.

## 2. Source of Truth

- `PRODUCT.md` and `DECISIONS.md` are the ratified product truth. `STATE.md` is the only live status record. Git history is the implementation record.
- Authority when sources conflict: the user's explicit current instruction → `PRODUCT.md` and `DECISIONS.md` → everything else. Chat transcripts, memory, and prior prompts are not authoritative.
- Anything not decided in `DECISIONS.md` is open. Do not assume a stack, schema, or feature the sources left open; propose it with tradeoffs, or make the smallest reversible choice inside the approved task and say so in your report.
- Surface conflicts between a task and repository truth. Never reconcile them silently, and never edit `PRODUCT.md` or `DECISIONS.md` to match code — the reverse is the rule, and changing them requires a user-ratified decision.

## 3. Scope Guardrails

- Build only the approved MVP in `PRODUCT.md`: one replayable decision class (account prioritization), the required experiences, the canonical fixture, the seeded dataset, and the acceptance criteria. Nothing in the non-goals list (`PRODUCT.md` §3) enters the build.
- The fictional operating company is **RelayBridge**; the canonical prospect is **NovaSignal AI**; **Merge** is a public reference archetype only and never a tenant, customer, or ledger account. Older "Acme Robotics" examples are superseded — do not reintroduce them.
- Shared demo constants (scores, thresholds, timestamps, costs, versions) come from one canonical source in the codebase. Never duplicate them by hand.
- Prefer the smallest complete vertical slice over infrastructure. Deepen the prioritization path before widening anything.

## 4. Semantic Guardrails (the invariants you must not break)

Full text is in `PRODUCT.md` §5 and §10. In practice:

- **Never let the present rewrite the past.** Recorded decisions, their preserved context, consumed inputs, logic identity, score, and output are immutable (INV-01). Corrections, outcomes, and new logic append; they never mutate (INV-01, INV-04, INV-08).
- **Seal context at the decision boundary.** Evidence available after `T(d)` never enters reconstruction or replay, and replay never reads current account state or a live source (INV-02). Missing inputs stay missing — never defaulted from current data (INV-03, INV-09).
- **Keep input states distinct:** available-and-consumed, available-but-ignored, explicitly unavailable, absent (INV-03). The UI and aggregates must show the difference, not just color.
- **Logic is data with identity.** A version label is not identity; the declarative artifact hash plus evaluator version is. Verify before exact replay; fail explicitly when missing or mismatched (INV-05).
- **Original ≠ counterfactual.** Separate records, separate labels; counterfactuals never overwrite, never appear as events that happened, never enter actual-outcome statistics (INV-06, INV-10).
- **Explanation ≠ evidence.** Generated prose is labeled explanation, traceable to recorded inputs, and never the authoritative basis (INV-07). Confidence appears only when defined; the canonical decision emits none (INV-09).
- **Outcomes are later observations** with explicit type and window; credit flows only through the versioned attribution policy as `direct` / `inferred` / `unresolved` (INV-08).
- **The collector is the only door.** Versioned envelope, stable `event_id`, canonical-JSON idempotency, visible failure, no partial writes; the demo seed uses the same boundary (INV-11).

## 5. Synthetic-Data and Claim Honesty

- Every vendor-like event, account, decision, and outcome is synthetic. Label it that way in the product; never imply live integrations, real customers, or private knowledge.
- Insights language is descriptive (observed rate, association, sample size, window). Never "causal," "lift," or "proves." Planted dataset effects are demonstration checks, not findings.
- Do not describe the product as more than it is: it fully replays the account-prioritization decision class, deterministically, on synthetic data.

## 6. Verification Expectations

- A change touching evidence, time, decisions, replay, explanations, actions, outcomes, or Insights is not done until the affected invariant IDs are named, the relevant acceptance criteria have automated tests, and failure paths are visible rather than silently repaired. Property-based or generative tests are required where AC-18 says so.
- Report exact test commands and results. "All tests pass" without naming the suite and the behavior it covers is not evidence. Pin completion claims to a commit.
- Passing UI or happy-path tests does not satisfy the invariant gate.

## 7. Repository Hygiene

- Keep `main` stable; use short-lived branches for meaningful work; make small, intent-named commits; do not mix unrelated refactors with behavior changes.
- Do not edit files another agent is actively editing unless the task is a deliberate comparison.
- Update `STATE.md` when phase, objective, blockers, or the next action change. Keep it short; it is not a backlog, roadmap, or branch log. Do not create parallel trackers.
- Never commit credentials or real personal data.

## 8. Stop Conditions — ask the user before

- changing the approved MVP, primary user, core promise, decision class, a core invariant, a canonical score, or a completion gate;
- adopting a high-lock-in architecture or a consequential new dependency;
- merging when the merge is consequential, deploying, running a destructive migration, deleting data, or removing major functionality;
- presenting a synthetic, observational, or counterfactual result as something stronger.

Escalate for product judgment (via the coordinating Claude session and, when needed, the ChatGPT review layer) when implementation exposes unresolved product behavior, architecture with product consequences, or a possible invariant conflict. Do not stop for ordinary reversible choices inside an approved task.

## 9. Responsibility Boundaries

The user holds final authority over product direction, scope, and consequential actions. ChatGPT is the product-intent and adversarial-review layer; the coordinating Claude session translates accepted intent into scoped tasks and keeps `STATE.md` current; coding agents implement and verify. No agent — including the one reading this — has final product authority, and no agent maintains a copy of state outside this repository.

## 10. Build and Review Handoffs

- Use the repository's existing, Git-ignored `.handoffs/` directory for scoped tasks, review packets, and reviewer dispositions in both directions. The user passes an absolute file path and review anchors between sessions. Read accessible files directly; if a session cannot access a path, say so and request only the missing material. Access in one session does not establish access in another. Do not contact or trigger the other model without explicit user authorization.
- Read committed code and requirements at explicit full commit IDs, using GitHub or a verified local checkout. A branch name alone is not a review anchor. Keep independent test execution isolated from an actively changing builder checkout and use disposable test data.
- A review packet retains the existing required evidence: intended behavior, approved requirements and invariant IDs, acceptance criteria and test mapping, scope limits, known gaps, coordinator recommendation, and decision requested. Include the repository, full comparison-base and review commit IDs, working-tree condition, approved task path and revision (or content fingerprint), and the specific `STATE.md` commit if different. Link the exact CI run when applicable and verify the commit it tested. Reference accessible files and logs instead of copying them. Identify later material requirement changes explicitly; never silently combine requirements from different versions.
- Label evidence accurately: code inspected; builder/coordinator results reported; CI results and logs inspected; tests independently executed by the reviewer. Reading a test or a passing CI log is not independently running it. A pre-dispatch review pins the repository context and draft task revision; existing CI does not prove unbuilt behavior.
- Return one disposition pinned to the reviewed commit, with numbered actionable findings, evidence, and closure criteria where corrections are needed. A correction handoff identifies the previous reviewed commit, corrected commit, new test evidence, and the response to each finding. Review the delta and affected behavior; expand when changes or unresolved concerns justify it. Preserve required checks and completion gates. Later unrelated changes do not erase a historical acceptance, but are not covered by it.
- The coordinator records durable conclusions in the existing repository truth files. Keep `STATE.md` focused on current status, outstanding limitations, and the next action; Git preserves prior states. Old packets are historical evidence, never current instructions or an additional tracker. Product authority, roles, pre-dispatch review, consequential merge approval, and deployment approval remain unchanged.
