# Eason One Current Product Checkpoint — OpenAI Research Handoff

Date: 2026-08-29
Status: WORKING CHECKPOINT — DO NOT INSTALL TO USER LIVE REPO YET

## Product movement in this checkpoint

This checkpoint moves the governed multi-Employee company runtime beyond a Perplexity-only Research path.

### 1. Researcher can use mature hosted search substrate from either provider

Formal RESEARCH Work may use:
- OpenAI Responses API hosted Web Search; or
- Perplexity/Sonar.

The Employee remains the persistent Researcher. The provider/model is only the current execution substrate.

### 2. OpenAI hosted Web Search is bounded by Work budget

A governed Work attempt permits at most one hosted web-search tool call. The configured request fee participates in the pre-approved Work/Project cost estimate and the actual AgentRun cost snapshot. Normal OpenAI calls do not accidentally inherit the hosted-search request fee.

### 3. Research success requires provider-observed sources

Research is not accepted merely because a model says that it searched the web. Runtime extracts provider-observed citations/search sources. A formal Research execution without usable source lineage is not treated as a successful researched result.

### 4. Research lineage survives company handoff

Accepted upstream Research Work projects structured provider source lineage into downstream Employee context. Product/Strategy/Engineer Work therefore receives the actual source lineage even if the human-readable Artifact body truncates or summarizes the citation list.

### 5. Project Outcome keeps source lineage

The accepted-output snapshot used for Project outcome closure retains provider source lineage from the producing AgentRun, so the company result does not sever Research evidence at final synthesis.

## Files changed since REAL_RESEARCH_COMPANY checkpoint

- `eason_one/providers.py`
- `eason_one/services/context.py`
- `eason_one/services/costs.py`
- `eason_one/services/execution.py`
- `eason_one/services/execution_policy.py`
- `eason_one/services/operations.py`
- `eason_one/services/project_outcome.py`
- `eason_one/services/task_execution.py`
- `eason_one/templates/hq_models.html`

## Local-repo warning

This is a construction checkpoint only. The user's actual local Eason One repository remains on the previously installed version until an explicit installation instruction is given and executed successfully.
