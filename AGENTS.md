# AGENTS.md: Flight Recorder Working Rules

Adopted 2026-10-28 by D-028 with the enterprise product contract. The former rules are preserved in Git history (last at `830ac4e106c2775a4a030e0e15974d3e573d8fa5`).

## Goal and authority

Build a decision-observability product that real enterprise sales teams can use. PRODUCT.md defines supported behavior and release gates. The owner's current explicit instruction takes precedence over PRODUCT and DECISIONS; surface conflicts and record adopted changes rather than silently blending old and new scope.

Read current PRODUCT, concise STATE, relevant decisions, and focused code/tests. At a session transition also read `.handoffs/continuations/LATEST.md` and verify current truth. Do not reread the entire project or rerun qualification as a startup ritual.

## Product rules

- Preserve historical inputs, evidence versions, recorded outcomes, and supported logic/evaluator identity. Current evidence never enters a historical reconstruction.
- Keep available/consumed/ignored/unavailable/absent inputs distinct. Keep originals, counterfactuals, external observations, and new simulations distinct.
- The authenticated, company-scoped collector is the only admitted domain-event boundary. Invalid/conflicting events have no partial domain effects. Legacy schema bytes and canonical fixtures remain unchanged unless a material change is explicitly adopted.
- Every company boundary is enforced on reads, writes, jobs, caches, exports, connectors, and service principals. Client-supplied identifiers never grant authority.
- No credential/customer data in Git, public demos, logs, or AI prompts by default. Do not access another project's runtime or original private stores to diagnose an application bug.
- Synthetic examples remain clearly labeled. Exact replay is promised only where the execution artifacts support it. Do not invent customer use, compliance status, causality, remote CI, deployment, or provider qualification.
- CRM writes and external execution default off. A counterfactual never writes to a CRM. An uncertain external operation stays uncertain and cannot be silently retried or credited as success.

## Ownership

The owner decides product direction and consequential actions. The coordinator maintains current STATE and material DECISIONS. One builder owns an active implementation change; a nonauthor reviewer independently reviews consequential semantics and security behavior. Direct communication among current Flight Recorder project chats is authorized without using the owner as a relay. Refresh actual role/session metadata and persisted context before routing a handoff.

Use the in-chat browser for previews and browser checks. Native desktop or native Chrome control requires a later explicit owner request; connector failure does not grant that permission.

The separate public-site checkout still follows its applicable CODEX rules. No actor silently edits another project's files or another active builder's work.

## Codex, the public-site design builder

Codex makes design edits to the public website (design, layout, styles, the code a design needs and the tests that cover them) under D-022 and D-023, in its own clone of this repository, following `CODEX.md` at the repository root. Codex merges its own design pull requests after the full check and CI pass; Claude reviews one only when the user asks; the user deploys. Codex does not edit `PRODUCT.md`, `DECISIONS.md`, `STATE.md` or `.handoffs/`, and other agents change `CODEX.md` only through a pull request the user approves.

## Delivery

Follow the risk table in `docs/DELIVERY-PLAYBOOK.md`. Produce one scoped outcome with acceptance criteria, implement, test appropriately, review according to risk, reconcile docs, commit, publish under approved branch policy, and verify CI on the exact revision. A passing local run is not remote CI; a push is not a deployment.

Ordinary in-scope test failures, bug repairs and reversible implementation choices are part of the approved work. Investigate and correct them without routine owner reapproval. Never weaken a test or silently alter semantics to make a gate green. Preserve failures and distinguish tooling errors from product failures.

Use focused local tests plus required PR CI. Preserve shared stylesheet/public-demo boundaries on presentation changes. Require independent review for historical semantics, company isolation, authentication/permissions, secrets, migrations and external writes. Repeat a full qualification only when changed behavior or unresolved evidence requires it.

Make small coherent commits including related documentation; never sweep unrelated untracked work into a commit. Keep `main` stable. The standing feature-branch push/PR policy applies only after the owner adopts it. Explicit task prohibitions always override defaults. Never infer force-push, publication of private handoffs, visibility/security-policy changes, or production deployment authority.

## Truth and documentation

STATE is the only live status record: current objective/revision/CI, actual active work, blockers and one next action, roughly one screen. Historical detail belongs in Git and existing dated evidence. PRODUCT is the current contract; DECISIONS records material choices and explicit supersessions. README and setup guides change alongside the capability they describe.

Use one compact completion record for ordinary tasks. Detailed authority/secret/data-preservation receipts are reserved for work that requires them. Never invent a hash. Preserve original bad evidence and append a correction. “Reported,” “inspected,” “independently executed,” “accepted,” “integrated,” “pushed” and “deployed” are separate claims.

## Actual owner decisions

Ask before a material change to the promise, supported decision semantics, company/data/security boundary, high-lock-in dependency or architecture, spending, production deployment, real customer data/provider writes, destructive migration, major removal, or access-policy change. Complete the reviewable preparation first and request the smallest unresolved decision. Do not ask again about an approval already given.

Stop an affected operation on actual permission denial, uncertain live side effects, unexpected retained-data mutation, stale authority, or uncontrolled resource use. Preserve evidence and continue unaffected authorized work. Do not bypass host approval, provider controls, or registered workflow guards.

## Continuity and user communication

Warn before context becomes unreliable. Save and verify a dated project-local continuation with current revision, permissions, original running handles/PIDs, failures, limits and next step. Refresh the intended receiver, verify actual pickup, and transfer exclusive ownership before ceasing competing work. Do not create recursive successors or restart jobs during renewal. Workflow/runtime installation or maintenance is a separately scoped operation, not a prerequisite for routine code changes.

Explain plainly what the user can do now, how it was verified, where it is committed/pushed, what remains, and who acts next. State running/finished/blocked and whether user input is needed. Continue approved work; stop only for a genuine blocker or material decision.
