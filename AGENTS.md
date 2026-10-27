# AGENTS.md — Operating Rules for Coding Agents

This repository builds **GTM Flight Recorder**, observability for automated revenue decisions. These rules apply to every coding agent (Claude Code, Codex, or any other) working here.

## 1. Start Here

Read, in order: this file → `PRODUCT.md` → `STATE.md` → `DECISIONS.md` (when the task touches a prior material choice) → the relevant code and tests. Do not reread the whole repository or produce a status ceremony each session.

**Mandatory session continuity:** After the truth files, read `.handoffs/continuations/LATEST.md` if it exists and follow its pointer to the latest dated continuation packet. Apply §13 in every session. Warn the owner early when context is becoming crowded, save a verified continuation packet in this repository, and give its absolute path in a copy/paste box before recommending a fresh session.

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

## 11. Codex, the design builder

Codex makes design edits to the public website (design, layout, styles, the code a design needs and the tests that cover them) under D-022 and D-023, in its own clone of this repository, following `CODEX.md` at the repository root. Codex merges its own design pull requests after the full check and CI pass; Claude reviews one only when the user asks; the user deploys. Codex does not edit `PRODUCT.md`, `DECISIONS.md`, `STATE.md` or `.handoffs/`, and other agents change `CODEX.md` only through a pull request the user approves.

## 12. Separately approved v2 product workflow

- The owner accepted the corrected practice-plan bundle, D-024 planning phase and incoming-only first-build scope on 2026-10-18 with the exact reply "Yes approved". Retain the request/reply provenance and hashes in `docs/planning/owner-approval-20261018.json`; do not treat that reply as workflow installation, provider, spending or deployment authority.
- For this separately scoped product work, Codex coordinates `/Users/frank/code/Flight-Recorder`; the coordinating Claude session translates accepted intent and records durable product truth. Section 11 and CODEX continue to govern the separate website clone. This section does not transfer product authority to a builder.
- The builder implements the exact accepted task in an isolated candidate. The lead owns deterministic tests because the selected worker route has only Read/Edit/Write. A fresh nonauthor reviewer assesses exact bytes, requirements, test evidence and failure paths. The lead integrates only reviewed bytes and verifies the integrated result. Worker completion, checker success, review, integration and owner acceptance remain separate.
- Deliberate workflow adoption may select frozen release `1.10.3-metadata-compatibility-candidate`, portable digest `8a7df877fc92d2630fddc7665485359f0666490bbfa1ecd75cc71a89b7cf8e41`, archive SHA-256 `1fed2608049c4bdc7269b30bf56a7bde90cc82d948043217ff82a7b458140142`. All 23 runtime scripts match the historical exact1f5538 schema2 Claude trial; this metadata successor is not a new provider trial. That historical proof and this project's disposable compatibility evidence are distinct. Neither supplies installation authority or current readiness. Preserve source03 and its finished fake qualification unchanged.
- The proposed first execution uses schema2 `parallel-claude`, one entry, cap1, max_launches1, pause on failure, exact `claude-opus-5-5` config/argv/init binding and bare Read/Edit/Write only. No Bash, MCP, browser, shell fallback, native adapter substitution, retry/resume or unattended scheduling. File scopes are work contracts, not an OS sandbox.
- Before adoption, the lead binds the actual installation identity, exact source/index/runtime and local guard/config/task/output/context/environment reconciliation; obtains the exclusive idle maintenance boundary and normal host approvals; preserves before bytes/modes, fresh preview and recovery receipts; verifies only reviewed changes and independent installed acceptance. An unknown/active runtime or changed registered enforcement fingerprint is a hold. Never edit registration, widen trusted hashes or normalize away guards to proceed.
- Use only the selected existing Claude.ai Max route and its fresh auth/model gates. This procedure grants no account/configuration change, new credential, API-key route, alternate provider, purchase or deployment. The current owner instruction governs ordinary authorized work; report any genuinely unavoidable new permission issue through the parent.
- Every dispatch binds the accepted task hash, full base/review commits, isolated root, source/runtime/executable/model/queue/config/context/output inventories, expiring queue-specific authority and actual host approval. Fresh preview, auth and launch gates must remain intact. Numeric ownership uncertainty, permission denial, input drift or cancellation holds the queue; preserve evidence and never infer retry permission.
- Follow section 10 for task and review handoffs. This first build begins with FR-V2-01 local synthetic capture; collector schema changes receive their own review. All original invariants, synthetic labels and read-only public-demo protections remain. Score write-back, real accounts/connections, spending and deployment remain separate.
- Training stays in the Flight-Recorder Claude project. The owner keeps scope, consequential merge and deployment authority. Rules do not install a workflow, start a model or ratify an implementation.
- An explicit action limit in the current scoped handoff overrides routine role conventions. Recording a decision or editing documents never implies commit/push permission when the handoff excludes Git mutations. Do not infer extra install, dispatch, publication or recovery actions; acknowledge a requested stop and wait for the next exact scope.

## 13. Mandatory context monitoring and session handoff

Owner instruction, 2026-10-18: this rule applies to every session working in this project, including a fresh session and work continued after automatic compaction.

- **Check capacity proactively.** Assess context pressure after meaningful milestones and before a large new step or large tool output. Use a reliable exposed context meter when available; around 70–75% used is an early checkpoint, sooner for complex work. Without a reliable meter, do not invent a percentage: use conversation length, anticipated or completed automatic compaction, repeated rereading, difficulty retaining constraints, or unresolved parallel threads as reasons to recommend a fresh session. Do not wait for a hard limit or deteriorating work.
- **Alert and preserve together.** When recommending a fresh session, first save the current continuation packet, then tell the owner briefly why a fresh session is advisable and provide one copy/paste box directing the new session to the absolute path of `.handoffs/continuations/LATEST.md`. If a recommendation must be made before writing is possible, state the problem and finish the checkpoint as soon as access permits. Do not start another large step before preserving the handoff. The owner may choose to continue; this rule does not create a project hold or override a current instruction.
- **Use one stable entry point and dated snapshots.** Store each packet in a new dated directory under `.handoffs/continuations/`, with a short `CONTINUE.md`, a factual `HANDOFF.md`, and a content-hash manifest for its evidence. Update `LATEST.md` to point to the completed packet only after verification. Preserve older packets; never turn them into competing live state records. `STATE.md` remains the only live status record and retains its existing writer/role boundaries.
- **Preserve actionable context.** Include the goal, current request and authorization limits, repository/project identity, full base/review/accepted commits, branch/worktree and dirty-file condition, decisions and their source paths, completed work, exact test commands/results and evidence limits, unresolved findings, next useful action, active owned processes/leases and safe cleanup/recovery facts, relevant coordination boundaries, and durable artifact paths. Separate reported evidence from independent verification. Do not invent approvals, include secrets, or dump the entire chat in place of a useful handoff.
- **Keep evidence inside the project.** Copy necessary evidence that otherwise exists only in `/tmp` or another disposable location into the dated packet, preserving original bytes and recording source-to-copy paths and hashes. Do not copy credentials, live data or large disposable databases. Reference existing durable project artifacts instead of duplicating everything. Identify anything inaccessible or intentionally omitted and its impact; do not claim lossless preservation without checking.
- **Verify before delivery.** Re-read the prompt and pointer, confirm their targets exist, verify copied evidence hashes and review anchors, and ensure the handoff names any status-record lag or unfinished action. On the next session, verify current Git/files against the snapshot before acting; report drift and prefer the current user instruction and ratified truth. A historical handoff never grants installation, dispatch, merge, spending or deployment authority.
- **Make startup reliable.** Every session must read this rule and the latest pointer at startup. When creating an isolated worktree, pass these instructions and the continuation pointer explicitly if they are not present in its checked-out revision. The project-local files persist between sessions on this checkout; a new clone or another machine needs the rule and packet transferred explicitly. Do not rely solely on conversational memory, automatic compaction, or a promise to remember.

This is an enforceable operating instruction for agents, not a claim that context usage or automatic file loading is always observable. If an environment cannot read these files, report that limitation and request only the missing material rather than pretending continuity is established.

## 14. Standing owner instructions: keep approved work moving

Direct owner instruction, 2026-10-20. Apply in every future session, including after compaction. Read this alongside the current scoped task and §13.

- **Act on the next approved step.** Carry the already planned and approved project forward. Continue through ordinary implementation, verification, independent review, correction and handoff work that the owner has authorized. Do not stop at a plan, a status report or an offer to continue when useful authorized work remains. Do not make the owner manually prompt each routine transition.
- **Reuse approvals.** Do not ask the owner to approve the same action or settled choice again. Check the actual current instruction and existing authorization first. The owner explicitly approved sending the completed Task 3 packet to the existing “Resume Flight Recorder review” chat (`01a15088-d96b-7f52-8921-4254765f48de`); carry that review and its ordinary correction handoffs forward within the approved Task 3 scope. Record delivery, recipient progress and review acceptance separately.
- **Stop only for a concrete reason.** Pause when the owner asks, a clear blocker prevents further authorized progress, or a genuinely consequential new action requires owner judgment. First finish unaffected authorized work and prepare a concrete, reviewable next action. Do not invent extra approval gates from routine role conventions. Product scope, explicit action limits and required verification/review gates still govern; this instruction does not silently approve deployment, spending, account access or destructive actions.
- **Respect unavoidable system gates.** Never bypass a security safeguard or an automatic approval rejection. If a system requires fresh explicit approval despite existing project direction, identify the exact blocked action and stated reason, ask only the necessary question, preserve the evidence and continue unaffected work. Do not describe the agent's own hesitation as a system requirement.
- **Always give a next direction.** Every user-facing reply ends with a clear next action, recommendation, or statement of the action already underway. Prefer starting the next authorized action during the same turn. If blocked, name the blocker and the smallest concrete action needed to remove it. A completion report by itself is not an adequate handoff.
- **Keep browser work inside the chat.** Use the in-chat browser for previews and browser checks. Do not take control of the owner's Mac or native Chrome merely because a connector fails. Diagnose the in-chat/local-preview issue within the approved scope; report a concrete blocker if it cannot be resolved. Native desktop control requires a later explicit owner request.
- **Keep communication useful.** Use concise, plain full sentences and meaningful progress updates. Preserve the distinction between implemented, tested, independently reviewed, accepted, integrated and released. Maintain durable continuity so the owner does not need to repeat these instructions in another session.


## Standing owner authorization: direct Flight Recorder chat coordination

Direct human instruction, October 22, 2026 (recorded October 23 UTC): “send the checkpoint to the builder, you no longer need permission to send to this projects chats, make sure the rule is changed, you should work together without needing me in the loop.”

Within this Flight Recorder project, existing builder, reviewer and coordinating Cowork/Claude chats may directly exchange handoffs, questions, findings, acknowledgments and ordinary scoped correction requests. Do not ask the owner to approve each intra-project message or manually relay it. This standing authorization supersedes earlier rules requiring coordination through the owner or fresh permission for direct model/chat contact. Preserve a durable record of material decisions and actual delivery/acknowledgment.

Cowork/coordinating Claude remains responsible for coordination and STATE.md; the builder handles implementation; the product-intelligence/review chat handles product judgment, independent review and the guided walkthrough. Approved work can continue autonomously. This authorization changes communication routing, not product scope, security/credential access, CRM write authority, merge/push/deployment approval, spending, or native desktop-control limits. Those boundaries remain in force. Do not create a parallel status tracker or treat message delivery as acceptance.

## Session handoff routing and automatic continuity (owner instruction, 2026-10-23)

1. Before every project handoff, refresh the receiving project's supported session list or metadata and latest persisted handoff. Verify the most recent relevant session for the intended project, task and role, and check its current active owner. Never blindly reuse a saved session ID, pinned chat or URL: a fresh successor may exist. Recency alone does not establish relevance. Preserve existing work and avoid duplicate ownership.
2. When a fresh session is needed and supported, first persist and verify the project context, current state, instructions and complete continuation. Initiate the successor in the same project, task and role without requiring the owner to create the chat or relay the prompt. Verify that the receiving session has loaded the persisted information and confirm active ownership before continuing; the predecessor must stop competing work after transfer. Carry current restrictions, approvals, consumed attempts, failures, STOPs and budgets forward accurately. A new session grants no new authority and resets no limit. If creation or verified pickup is unavailable, preserve the checkpoint and report the limitation. Do not start fresh sessions merely to demonstrate this rule.

For session transitions, this newer instruction supersedes older manual-relay-only directions. All other project roles, scope and action boundaries remain in force.

Continue to inform current collaborators of a verified successor and record destination, freshness, delivery and pickup separately. If automatic renewal is unavailable, provide the saved absolute-path continuation prompt.
