# Headquarters V0.10.3 — Authoritative CEO Run Repair

Date: 2026-08-02

## Defect reproduced

Founder request:

> Review the current Eason One project status and prepare a concrete three-step plan for the next development task. Do not modify any files yet. Define the objective, acceptance criteria, risks, and which employees should participate.

V0.10.2 classified the mixed request as a local BRIEF because it contained the word `status`. No Provider Run was created. The V0.10.2 visual layer then intercepted the form and called a second guessed route whose resolver searched for `founder_request()` in the wrong services, producing `founder_request() could not be resolved`.

## Architecture correction

1. The existing Headquarters runtime remains authoritative:
   `/headquarters/ceo/preview` → `/headquarters/ceo/execute` → `services.ceo.founder_request()` → `services.execution.execute()`.
2. The V0.10.3 visual layer no longer installs a submit handler, calls a second runtime route, or stops propagation.
3. Status routing is now deliberately narrow. A message that also requests a plan, analysis, improvement, implementation, team, risks, or acceptance criteria enters the governed Run path.
4. Pure company-status lookups remain local and do not create a paid Provider Run.
5. The CEO prompt now requires the visible reply itself to summarize objective, ordered steps, proposed Employees, material risks, and acceptance criteria when the Founder asks for a plan.
6. UI reconstruction retains the accepted Mission Room language while removing duplicate headings and preserving the tested native dialogue thread, Run link, approval card, technical audit, and operation runner.

## Auditable validation

The V0.10.3 installer runs an isolated mock-provider database test and verifies:

- the exact failed Founder request routes to `ACT`;
- preview says execution is required;
- the canonical execute endpoint persists a `CEO_FOUNDER_REQUEST` AgentRun;
- provider/model snapshots, token counts, cost, output, and completion timestamp are stored;
- a governed Operation proposal remains `PLANNED` and is not auto-approved;
- Founder and CEO dialogue records are persisted;
- the old compatibility route can resolve the real CEO service;
- a pure company-status request still remains deterministic and creates no Run;
- Headquarters renders one V0.10.3 stylesheet and one V0.10.3 script;
- the V0.10.3 layout script contains no legacy runtime fetch, no `stopImmediatePropagation`, and no duplicate runtime implementation.

Relevant regression suite result in the repair workspace: 22 passed.
