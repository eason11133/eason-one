# Eason One Headquarters V0.7 — Dogfood Reliability & Research Capture

## Product goal

This release moves Eason One away from a visual multi-agent demo and toward a system the Founder can use to complete a real project before 2026-09-30.

The target closed loop remains:

Founder Mission → CEO plan → Founder authority → Task assignment → Employee execution → bounded Meeting when needed → reviewed result → auditable history.

## What V0.7 changes

### 1. Precise Meeting prompts

Every autonomous Meeting contribution now uses a versioned prompt contract with explicit sections:

- ROLE
- CURRENT TURN
- OUTPUT CONTRACT
- COMPLETION STANDARD
- PROHIBITED

The prompt forbids essays, repeated background, invented evidence IDs, hidden state mutation, and text outside the required JSON object.

### 2. Cross-round discussion continuity

The full transcript remains in SQL for audit. Each speaker receives a bounded active packet containing:

- current Meeting question;
- structured live state;
- recent validated contribution deltas with Meeting Message IDs;
- visible Company Brain evidence IDs;
- latest Founder intervention;
- invited Employee slugs.

This makes later speakers respond to prior validated work without replaying the entire transcript.

### 3. Compact repair instead of blind replay

A paid contribution that fails because of truncation or invalid structured output can receive one explicit Founder-authorized repair. The repair:

- links to the failed AgentRun;
- uses a smaller output ceiling;
- uses the same exact schema;
- removes repeated background;
- produces a fresh complete object instead of replaying the failed essay.

Failed calls remain visible and remain included in cost totals.

### 4. Idempotent Meeting Start

Repeated Start on an already-running Meeting no longer creates a false red failure. It returns the existing running Meeting so the Founder can attach the live runner.

### 5. Honest call and cost accounting

Meeting UI and minutes separate:

- total provider calls;
- successful calls;
- failed calls;
- retries;
- truncations;
- tokens;
- real cost.

The failure panel shows Employee, provider, model, stop reason, charged tokens/cost, retry strategy, retry estimate, and the failed Run link.

### 6. Governed Employee AI Core switch

People → Employee Dossier → AI Core now includes a Founder control to change the Employee's active ready ModelConfig. The Employee identity stays unchanged and an EmployeeModelHistory row is written.

### 7. Research Ledger inside Eason One

The new Research surface is derived from persisted operational state without a provider call. It includes:

- provider-call metrics;
- first-pass success;
- retry/truncation counts;
- token and cost totals;
- prompt-version performance;
- Meeting-level experiment ledger;
- failure ledger;
- Founder evidence/experiment records;
- full JSON export;
- AgentRun CSV export.

AgentRun now records prompt version and SHA-256 hashes for the prompt, context, and output, plus retry lineage.

### 8. Less repetitive Headquarters reports

When a deterministic CEO status report is the lead block, Headquarters no longer repeats the same people, next-result, and recent-change facts as equally prominent cards. The current Mission remains the secondary source of truth.

## Data integrity rules

- SQL remains authoritative.
- ResearchRecord stores Founder interpretation; it does not overwrite raw Run, Meeting, Task, cost, or Company Brain evidence.
- Old runs remain visible as unversioned/incompletely captured historical records.
- Full exports may contain sensitive prompts and context. Keep them private unless reviewed and redacted.
- No `.env`, credential, production database, or API key is included in this patch.

## Not implemented in V0.7

- Full Claim Blackboard persistence as first-class tables.
- Learned sparse routing.
- Automatic external benchmark generation.
- Complete Artifact entity/version store.
- Eason Market salary, wallet, payroll, spinout, or inter-company settlement.
- Public hosting or multi-user security.

Those remain later work. V0.7 prioritizes reliable dogfooding and evidence capture.
