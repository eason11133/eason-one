# Eason One Headquarters V0.6

## Purpose

Headquarters is the Founder-facing desktop product surface. It translates the governed runtime into focused company rooms and a CEO-first arrival briefing instead of a single dense dashboard.

- Desktop Headquarters: Founder is physically present at company HQ.
- Mobile CEO Line: Founder is away and requests a persisted-state briefing.
- Legacy Command, Work, Team, Company, Project, Meeting, Employee, Model, Cost, and Run pages are retired from product navigation. Their data and deterministic POST/action endpoints remain connected.

## Product rooms

| Route | Responsibility |
|---|---|
| `/headquarters` | CEO arrival briefing, priority Mission, Founder authority, and room entry |
| `/headquarters/missions` | Mission registry by operating state |
| `/headquarters/missions/<id>` | Mission contract, Tasks, gates, handoffs, review, artifacts, provenance, and explicit Operation budget |
| `/headquarters/people` | Departments and persistent Employee directory |
| `/headquarters/people/talent` | HR Talent Catalog, verified role-source import, and staffing requests |
| `/headquarters/employees/<id>` | Complete Employee Dossier |
| `/headquarters/meetings` | Meeting lobby and Meeting creation |
| `/headquarters/meetings/<id>` | Spatial Meeting Room using the real Meeting runtime |
| `/headquarters/results` | Founder-ready result review |
| `/headquarters/attention` | Governance gates and incidents that actually block current work |
| `/headquarters/memory` | Effective Company Brain records by kind and scope, with exact presentation duplicates hidden |
| `/headquarters/finance` | Real spend, Mission budgets, and explicit separation from the not-yet-operational Employee economy |
| `/headquarters/system` | Runtime control-room index and historical reliability visibility |
| `/headquarters/system/models` | Model/provider configurations through existing APIs |
| `/headquarters/system/runs` | AgentRun list, grouped failures, and raw audit |
| `/headquarters/system/runs/<id>` | Raw technical execution audit |
| `/mobile` | Remote CEO Line, Mission/Employee progress, and Meeting visibility |

## V0.4.1 rules

### Source-safe language

The Headquarters product shell and controls are English-only. Persisted source records are never stripped, rewritten, or translated destructively. Chinese Mission titles, Task descriptions, acceptance criteria, evidence, and artifacts remain exactly as stored. An English display summary may be used only when it exists as a separate field.

### Founder attention vs reliability backlog

A failed AgentRun appears as a blocking incident only when its linked active Task, Operation, or Meeting is presently blocked or paused. Old failed runs remain auditable in System and Agent Runs, but they do not put the CEO or Founder shell into emergency mode.

### HR and the 147-role library

The 147 roles are a Talent Catalog, not 147 hired Employees. The system imports only a verified JSON/CSV role source found at the repository root or `data/`. When that source is absent, Headquarters says so and does not invent role records. HR can create governed staffing requests from the catalog; hiring materializes a new persistent Employee only through the existing approval flow.

### Economy honesty

Real API/tool spend and Mission authorization are operational. Employee wallets, overtime, purchases, Eason Market, and company formation are not operational yet and must be labeled as such. A configured salary-credit schedule is not presented as a real wallet balance.

## Legacy route retirement

Legacy GET routes redirect into their corresponding Headquarters room. Existing mutation endpoints remain authoritative and are used by the new forms.

Examples:

- `/command` → `/headquarters`; POST still reaches the governed CEO runtime.
- `/work`, `/projects` → `/headquarters/missions`.
- `/team`, `/employees` → `/headquarters/people`.
- `/team/hr` → `/headquarters/people/talent`.
- `/meetings` → `/headquarters/meetings`; POST still creates a real Meeting.
- `/company/brain` → `/headquarters/memory`.
- `/models` → `/headquarters/system/models`; POST still creates a real model configuration.
- `/costs` → `/headquarters/finance`.

## Read-model boundary

`eason_one/services/headquarters.py` projects persisted records into screen-specific view models. GET requests do not invoke a provider. The sole retained legacy-recovery path reconstructs one known pre-V1 structured Operation record without a model call.

A CEO briefing reads persisted state and creates no AgentRun. A CEO command uses the existing governed `founder_request` runtime.

## Not fabricated

WorkSession, scheduling, overtime, payroll wallets, Eason Market, background workers, and multi-company execution are not shown as active systems until their real backend state machines exist.

## Verification

```powershell
.\scripts\verify-headquarters.ps1
.\scripts\run-headquarters.ps1
```

## V0.5 Founder-reviewed interaction rules

### Adaptive CEO briefing

`/headquarters` is not a fixed dashboard. The read model deterministically selects one composition from persisted company state, in this priority order:

1. Founder decision
2. Blocking runtime incident
3. Live Meeting
4. Founder-ready delivery
5. Normal Mission operation
6. Idle / next Mission

Each mode changes the primary layout and message. Room links and recent authoritative changes remain supporting context. The CEO orb is deliberately subdued and never carries state by animation alone.

### Meeting lobby versus live Meeting room

The Meeting lobby and planned-room preview remain spatial and show every invited participant. After a Meeting starts, the detail room switches to a maximum four-tile conversation surface:

- up to four participants are visible in a 2×2 grid;
- every participant keeps an independent dialogue tile;
- when more than four people attend, background participants remain rendered but hidden;
- the latest speaker is deterministically promoted into the first visible tile;
- Founder observe mode does not consume a participant tile.

### ModelConfig lifecycle

Archived ModelConfig records do not expose an illegal Enable action. They expose an explicit Restore action. Restoring makes the same historical configuration active again; Run history and Employee identity are not duplicated. A configuration assigned to active Employees cannot be newly archived until those Employees are reassigned. Provider credential health remains a separate readiness signal.


## V0.6 Founder briefing and status-report contract

### Ranked briefing composer

`/headquarters` is assembled from ranked briefing blocks on every render. It does not reserve permanent Mission, People, report, or metric-card positions. Founder authority, a blocking incident, a live Meeting, a delivery, the current Mission, active Employees, recent authoritative changes, and the latest validated CEO report each contribute a block with their own priority and footprint. The highest-value block determines the lead and the first-row geometry. Supporting context follows only when it is relevant.

A CEO report returned from `CALL THE CEO` becomes the lead block for that response. It is not inserted into a fixed dashboard slot.

### Deterministic whole-company status query

Whole-company status requests use purpose `CEO_STATUS_REPORT`. Before the provider is called, the backend creates an authoritative SQL snapshot containing exactly:

1. priority Mission;
2. current Mission stage;
3. Employees working now;
4. blocking incidents;
5. Founder actions required;
6. next expected result.

The model receives that snapshot and may author only one bounded `executive_summary`. Mission IDs, Employee names, counts, stages, incidents, decisions, and expected results are rendered from the persisted snapshot, not generated by the model. A request for the status of Eason One must never be reinterpreted as a search for a Mission named `Eason One`.

A syntactically valid provider response is not sufficient for success. Missing, malformed, or contradictory status output is recorded as `STATUS_REPORT_VALIDATION_FAILED`; it does not acquire authority and does not create a Mission, Task, Meeting, Employee, KnowledgeItem, budget, or configuration.

### Prepared versus live Meeting presentation

A `PLANNED` / prepared Meeting remains an all-participant spatial preview. It shows each invited participant, name, role, and the central issue, but no dialogue boxes or placeholder speech. Only a running Meeting switches to the bounded four-tile conversation surface and latest-speaker promotion behavior.
