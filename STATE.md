# STATE.md: Current Repository State

> The only live status record. Keep it short and current: the objective, what exists, the limits, open owner decisions and one next action. Git holds prior versions of this file; `DECISIONS.md` holds ratified decisions; the git-ignored `.handoffs/` folder holds dated evidence, never current instructions.

**Updated:** 2026-10-28 UTC. D-028 adopted: enterprise destination, observability-first release, new `PRODUCT.md` and `AGENTS.md`. The former long STATE narrative is preserved in Git history (last at `830ac4e106c2775a4a030e0e15974d3e573d8fa5`).

## Current objective

Build Flight Recorder into a decision-observability product that real sales teams can use (D-028). In the first release, a company's existing sales tools keep scoring and routing accounts; Flight Recorder records those decisions, verifies them, makes them investigable and supports honestly labeled replay. The synthetic MVP and public demo are the completed foundation.

## Repository and checks

- `main` on GitHub and on the Mac: the D-028 adoption commit, on top of `830ac4e106c2775a4a030e0e15974d3e573d8fa5`.
- Last passing CI on code: run 40642132714 on `830ac4e` (lint, 2,530 general tests, 294 invariant tests, 1 inert skip). The Linux failures of 2026-10-27 were fixed in PR #9.
- Untracked local material (`.workflow/`, `CLAUDE.md`, `Claude outputs/`, `docs/planning/`) stays local and unpublished.

## What exists today

- Deterministic account-prioritization history: preserved evidence, versioned declarative logic, exact original replay, labeled counterfactual comparison, outcome attribution and synthetic Insights, on one SQLite database.
- One Salesforce proof (D-027): a read-only connection to an isolated developer org captured one fictional Account, replayed locally. The score-writer code (D-026) exists, stays off and is outside the first release.
- A public, read-only synthetic demo at https://flight-recorder.app, running an older build. Nothing has been deployed since.

## Not built yet (required by the new PRODUCT.md)

Company workspaces, sign-in and permissions, company data isolation, continuous ingestion of an existing tool's decisions, production hosting, backups and monitoring, and use by a real team.

## Open owner decisions (each at its gate)

Deployment model; hosting, database and authentication providers; capacity, recovery and retention targets; a standing push and pull-request policy.

## Next Action (exactly one)

Codex lead: prepare the architecture proposal for the first private production slice (company workspaces, sign-in, company isolation and decision ingestion; `PRODUCT.md` §2, §5 and §6), with options and tradeoffs for the owner's decisions on hosting, database and authentication. Nothing is built, bought, connected or deployed until he decides.
