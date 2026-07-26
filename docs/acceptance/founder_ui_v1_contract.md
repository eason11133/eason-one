# Eason One Founder UI V1 Acceptance Contract

This contract is frozen before the Founder UI V1 implementation. It converts the supplied product design into executable structural requirements without claiming that automated checks prove subjective visual quality.

## Product definition

Eason One is the operating system of Eason’s AI-native Company. The normal Founder relationship is Founder → CEO → Company → Employees, Work, Meetings, and Decisions. AgentRun, ModelConfig, MeetingStep, structured JSON, provider settings, and related implementation details remain secondary Company or audit surfaces.

The experience must feel calm, premium, intelligent, operational, alive, and purposeful. It must not become a generic dark SaaS dashboard, Notion clone, Bootstrap admin, cyberpunk terminal, gaming HUD, card gallery, or neon toy.

## Preserved decisions and backend freeze

Preserve the approved dark blue/black direction, restrained cyan accent, four-level dark depth, abstract orbital CEO core, intentional Command whitespace, primary navigation of Command/Work/Team/Company, real organization hierarchy, terminal result-first Meetings, and secondary technical information.

Frontend templates, CSS, presentation partials, and read-only view composition may change. The implementation must not change OpenAI or Anthropic providers, AgentRun execution, costs, Company or Meeting budgets, Meeting execution or idempotency, paid failure or retry semantics, recovery, Company Brain or Proposal governance, CEO request modes, database models, migrations, background autonomy, or provider behavior. Normal page navigation remains zero-provider-call. No real provider call is authorized.

## Shared design system

Shared CSS defines and consistently applies four visibly distinct dark levels:

- `--canvas: #071018`
- `--surface-1: #0B1620`
- `--surface-2: #101E29`
- `--surface-3: #142733`

Shared primary, secondary, and muted text tokens remain approximately `#F4F7FA`, `#A1B3C2`, and `#6F8799`; cyan remains approximately `#43C7E8`. Canvas, section, raised object, and focused object must not visually collapse together. Page-specific arbitrary blacks are avoided.

Typography prioritizes Founder page titles, primary objects, section titles, body copy, then metadata. Uppercase is limited to compact telemetry, statuses, and small taxonomy. Buttons use one dominant primary action per context, raised or bordered secondary actions, text tertiary actions, and restrained red danger actions. Motion is limited to CEO orbit, online/live pulse, and small hover transitions, with reduced-motion support.

Desktop provides visibly usable persistent access to exactly Command, Work, Team, and Company. Meetings, Models, Costs, Runs, and Inbox remain under Company. Main content uses desktop space intentionally without meaningless empty columns or contentless black panels. Responsive layouts stack without horizontal overflow.

## Command

Command is the primary Founder experience, not a dashboard or ChatGPT clone.

Its order is:

1. Compact operational telemetry.
2. Founder greeting.
3. Central orbital CEO core and visible CEO state.
4. Integrated CEO command composer.
5. Executive Brief.
6. Active Company work and live operational activity.
7. Secondary recent CEO conversation.

The composer is visually connected to CEO. The Brief follows naturally. Intentional CEO whitespace is allowed, but Company state begins below without a marketing-landing-page feeling. Empty active work is calm and factual. Conversation uses compact transcript rows, no giant bubbles, raw JSON, or AgentRun IDs by default.

## Work

Work answers “What is my Company working on?” It uses conditional Founder Attention, wide Active Operations rows, On Hold, and Recent Results. Rows show business name, status, objective, next move, owner/team, exact deterministic progress when available, and spend. They never expose Task IDs, provider/model details, or invented percentages.

## Team and Employee identity

Team must feel like people operating a Company. It builds a real organization tree from Department, Position, and `Employee.manager_id`. A worker is structurally nested beneath its manager rather than rendered as a sibling with metadata only.

Each compact personnel node contains a lightweight person/avatar visual, Employee name, role, and textual deterministic status. Provider configuration is not primary. The same avatar language is used in Team, Meeting, and Employee detail.

Employee detail prioritizes name, role, department, status, current model display label, current work, recent contribution, Founder signals when available, and Interview. Provider IDs, model internals, prompts, costs, and execution audit remain under Advanced.

## Company

Company is the advanced systems hub, grouped as:

- Intelligence: Company Brain.
- Operations: Meetings and Founder Approvals.
- Infrastructure: Models, Costs and Budget, and Agent Runs.

Advanced pages use the shared shell, typography, surfaces, and button system without unnecessary staging.

## Meeting Lobby

The Lobby’s primary purpose is “What is my Company discussing?” rather than “configure a multi-agent API call.”

Its visible hierarchy is:

- Meetings title and business-language description.
- Secondary `+ New Meeting` action.
- Live Meetings.
- Needs Attention only when applicable.
- Recent Meetings/results.

Meeting records are full-width operational rows. Normal records prioritize Founder-facing status, title, participants, meaningful current topic or result, exact cost/tokens/calls, and Observe/View Result. They do not primarily expose chair, Company-level, terminal round counters, mock/provider identifiers, raw model IDs, or validation internals.

Manual creation remains the existing backend workflow but is closed by default in a secondary expandable planner. Participant identity is Founder-facing; mock/provider implementation details are secondary under Advanced.

## Active Meeting

Active Meeting uses a responsive approximately 65/35 Meeting Room and Live Summary composition. Participants use Employee avatars and identity. Natural-language speech is primary, with relationship markers and compact action/control/risk counts. POSITION, ACTIONS, RISK, RELATION, CORE POINT, CONTROLS, EVIDENCE, and validation remain secondary under collapsed structured-contribution details.

Live Summary uses persisted/deterministic state and prioritizes current topic, agreement, qualification/disagreement, key risk, next step, cost, tokens, and calls. Empty low-information fields are omitted. No blank third column or fixed narrow participant cards are allowed.

## Terminal Result mode

For `ENDED`, or `TERMINATED_BY_FOUNDER` with recovered intelligence, the Result appears before Discussion in DOM and visual order. Discussion is collapsed by default; audit is tertiary. No large empty Founder Control block renders when no action exists.

One main Result surface uses internal tonal hierarchy. Decision is strongest; qualification and risk are secondary; actions and controls are readable; metrics are compact. The complete Participants label remains inside its container without clipping or negative positioning.

Recovery truth remains explicit:

- Original status: TERMINATED BY FOUNDER.
- Recovery: LOCAL · 0 API CALLS.

Presentation must not imply normal completion.

## Founder-facing language

Primary surfaces prefer Result, Decision, Work, Team, Meeting, Review, Needs Founder, Cost, and Next Move. AgentRun, MeetingStep, response schema, provider snapshot, validation JSON, materialization, deterministic mock, and related implementation language remain Advanced/Audit terms.

## Frozen executable acceptance

A. Primary navigation visibly contains only Command, Work, Team, and Company.
B. GET `/command` invokes zero providers.
C. Command retains the CEO orbital core.
D. Team hierarchy follows actual `manager_id` nesting.
E. Employee nodes contain dedicated person/avatar visuals.
F. Meeting Lobby defaults to Meeting activity/history rather than the planner.
G. The New Meeting planner is secondary and closed by default.
H. Normal Meeting Lobby content does not expose LOCAL MOCK or DETERMINISTIC-MOCK.
I. Active Meeting renders Meeting Room plus Live Summary.
J. Contribution primary content contains natural-language dialogue.
K. Structured contribution is secondary and collapsed.
L. Terminal Meeting Result precedes Discussion.
M. Terminal Discussion is collapsed by default.
N. Terminal Meeting without actionable Founder controls has no empty control panel.
O. Recovered Meeting states TERMINATED BY FOUNDER and LOCAL · 0 API CALLS.
P. Full Participants label renders in Result structure.
Q. Work uses deterministic progress only.
R. Normal navigation invokes zero providers.
S. Existing Meeting recovery regressions remain green.
T. Existing two-person Economy execution remains unchanged.

## Visual authority

Automated tests prove structure, behavior preservation, and zero-call safety only. Codex may report `AUTOMATED CONTRACT: PASS` but must not claim Founder visual acceptance. The Founder decides the visual gate from real screenshots.

