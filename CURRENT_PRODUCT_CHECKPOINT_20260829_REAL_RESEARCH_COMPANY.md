# Eason One Working Checkpoint — Real Research Company

Date: 2026-08-29
Status: WORKING CHECKPOINT ONLY — DO NOT INSTALL TO LOCAL REPO YET.

## Product movement in this checkpoint

- A governed delivery Work now has exactly one accountable required capability. Materially different specialties must become separate Works so ownership, dependency, budget, review and recovery are explicit company truth.
- CEO planning is instructed to split current-fact research from downstream strategy/design/marketing work instead of asking non-research Employees to pretend they researched the web.
- CEO is no longer treated as an automatic delivery specialist fallback for missing Product/Operations capability. A real specialist gap goes through Team Formation + Hiring.
- RESEARCH Work now routes through an existing search-backed provider substrate (Perplexity/Sonar) rather than the generic one-shot LLM execution path. Employee identity remains persistent; the research model is only that execution core.
- Provider-observed citations/search results are captured by runtime, stored on AgentRun context lineage, surfaced in run audit, and appended deterministically to the Research Artifact handed to downstream Employees.
- Model-authored evidence URLs are not accepted as provider evidence when the provider supplied an observed source set.
- A live Research run with zero provider-observed sources is not treated as successful research.
- Independent review of Research Work also uses the research substrate and cannot accept an untraceable review with no observed sources.
- Search request cost is now part of governed pricing through ModelConfig.request_price_per_call, including historical AgentRun pricing snapshots and Founder-visible budget estimation.
- Perplexity/Sonar is not considered formally ready when request pricing is missing.
- Founder Project Work surface marks tool-backed executions, and Run detail shows tools + observed source evidence.

## Important boundary

This checkpoint does not claim every Employee has a tool loop. It specifically closes the first non-Engineering commodity execution gap by giving Research/Research-review a mature live-search substrate. Product/strategy/other Employees can consume accepted Research Artifacts while their own specialty tooling is added only when a real Project requires it.

## Local repo status

The user's local Eason One repo is still the older installed version. This ZIP is a synchronization checkpoint only.

## Files changed since prior Team Formation / Outcome checkpoint

- `eason_one/__init__.py`
- `eason_one/models.py`
- `eason_one/providers.py`
- `eason_one/routes.py`
- `eason_one/schemas.py`
- `eason_one/services/ceo.py`
- `eason_one/services/costs.py`
- `eason_one/services/current_company.py`
- `eason_one/services/execution.py`
- `eason_one/services/execution_policy.py`
- `eason_one/services/model_configs.py`
- `eason_one/services/operations.py`
- `eason_one/services/task_execution.py`
- `eason_one/services/team_formation.py`
- `eason_one/services/work_execution.py`
- `eason_one/templates/hq_models.html`
- `eason_one/templates/hq_project.html`
- `eason_one/templates/hq_run.html`
- `eason_one/templates/models.html`
- `tests/test_v020_multi_employee_company.py`
