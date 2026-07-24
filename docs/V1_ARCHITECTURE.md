# V1 Architecture

Eason One is a server-rendered Flask application with SQLAlchemy and SQLite. `create_app` owns configuration and database initialization. Explicit SQLAlchemy models preserve organizational identity, work, governance, executions, and ledgers.

The deterministic boundary lives in `eason_one/services`. Routes accept Founder actions but delegate project creation, task transitions/review, executions, budget calculations, model changes, interviews, and knowledge approval. LLM output is stored as an `AgentRun` or a pending `Proposal`; it cannot directly change authoritative Project/Task state or Company Brain truth.

Employees are persistent identities linked to replaceable `ModelConfig` records. Every change is appended to `EmployeeModelHistory`. Provider selection uses `provider_key`; V1 includes a deterministic Mock provider and an environment-backed OpenAI Responses adapter.

Execution builds a filtered text context from employee identity, optional project/task, recent task messages, current non-superseded Company Brain items, and remaining real budget. Projectless calls receive company-level knowledge only. Each call creates an immutable run attempt with provider/model/pricing snapshots. A conservative maximum-cost check runs before provider invocation; known actual spend is committed exactly once before downstream parsing. Retries create new runs.

Structured workflows pass strict JSON Schemas through the provider boundary to OpenAI Responses while retaining deterministic application validation. OpenAI response IDs and HTTP request IDs are stored separately; incomplete/refused billable responses retain token usage and exactly one ledger entry before being marked failed. Structured Task results can create pending knowledge Proposals, never authoritative knowledge. Founder-approved Decisions may link only currently effective same-Project or company-level approved basis items through `BASIS_FOR` references in the same transaction.

Task transitions use a fixed transition map. Execution leads to REVIEW; an explicit reviewer accept/reject action leads to DONE or WORKING. CEO JSON is validated against a narrow project-plan schema and materialized in one transaction. Proposal approval is an explicit Founder action through the same Brain validation path. Knowledge corrections and killed hypotheses are append-only and their replacement/warning records remain in effective context.

The Jinja UI exposes CEO command, project/task execution and review, persistent employee inspection/interviews, Founder Inbox, cost ledger, and run audit pages. No background execution is implied.
