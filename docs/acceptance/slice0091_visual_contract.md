# Slice 009.1 Visual Acceptance Contract

This contract is frozen before product UI implementation. Automated checks prove structure and safety only. They do not prove subjective visual quality; the Founder retains the visual gate.

## Product intent

The Founder should feel “I opened my AI company,” not “I opened a multi-agent admin dashboard.” The experience must feel alive, operational, calm, intelligent, and premium. The direction combines a restrained command center, Apple-like calm, operational density, and Linear-like polish. It must not resemble a generic SaaS dashboard, Notion clone, admin template, cyberpunk terminal, or gaming HUD.

## Command composition

The first desktop viewport prioritizes, in order:

1. Company operational status.
2. Founder greeting.
3. CEO as the dominant visual anchor.
4. Primary CEO command input.
5. CEO executive brief.
6. Active Company work and Founder attention.
7. Recent Company activity.

### A — CEO dominance

- `/command` contains a non-empty `.command-stage`.
- It contains a non-empty `.ceo-core`, visually central within the stage.
- The CEO core’s desktop target width is 180–240px.
- CEO state appears near the core.
- The main composer is directly associated with the stage.
- The stage precedes the executive brief in DOM order.
- The executive brief precedes lower Active Company and Activity content.

### B — CEO core

- HTML/CSS-only circular or orbital identity.
- Restrained radial depth and subtle cyan/white accent.
- Central CEO mark or initials.
- ONLINE or WORKING state.
- Minimal ambient CSS animation with reduced-motion support.
- No stock image, giant neon glow, spinning game HUD, flashing, canvas, or JavaScript animation library.

### C — command composer

- The dominant interactive control belongs visually to the CEO stage.
- It is substantially wider than ordinary controls.
- It uses a restrained single-command composition, preserves existing submit behavior, allows textarea expansion, and integrates the Send action.
- It must not read as a conventional card containing a heading, help text, textarea, and button.

### D — telemetry

- One compact `.telemetry-strip` uses deterministic persisted data only.
- It may show Company state, CEO state, active work count, active Meeting count, and today’s real spend.
- It must not use separate giant metric cards.
- Loading it costs zero provider calls.

### E — CEO brief

- `.executive-brief` is distinct from a generic SaaS card.
- It prioritizes CEO Brief, subject, state, executive summary, Next Move, Watch/Risk when available, and a review action.
- Typography, spacing, and separators carry more hierarchy than box borders.

### F — Founder attention

- When attention exists, render a restrained but interruptive `.attention-signal`.
- When none exists, render no giant empty attention placeholder.

### G — active Company

- Active work renders as `.operation-row` operational rows, not generic cards.
- Rows prioritize name, stage/status, objective, next move, and owner/relevant team.
- Command shows no more than three and no implementation IDs.

### H — live activity

- `.event-stream` reads as an operational timeline with human-readable `HH:MM` time and subtle event nodes.
- It must not display raw microsecond timestamps.
- Event language must be meaningful and supported by stored state; events must not be fabricated.

### I — recent CEO conversation

- Conversation history is secondary to the command stage and may be compact, bounded, or collapsible.
- AgentRun IDs and raw JSON are not shown by default.
- Founder and CEO remain distinguishable without giant messenger bubbles.

## Work visual contract

`/work` is a Company operations board, not a generic card grid. Its hierarchy is Work / Company execution, conditional Founder Attention, Active Operations, On Hold, and Recent Results. Active work uses wide `.operation-row` elements with name, state, objective, next move, owner/team, and real progress only when available. Progress is never invented.

## J — Team is an organization

- Desktop `/team` uses `.org-chart`, `.org-branch`, and compact `.employee-node` structures.
- CEO appears at the top; branches appear beneath.
- Hierarchy is derived from Department, Position, Employee, and `manager_id`; employee names are not hard-coded to determine structure.
- CSS connecting lines visually express reporting structure.
- Founder-facing status is WORKING, IN MEETING, AVAILABLE, or DISABLED.
- Mobile may collapse to department sections.

## K — Employee detail

Employee detail resembles a personnel dossier. It prioritizes name, role, department, status, current model display name, current work, recent contribution, Founder feedback when available, and Interview. Exact provider and model internals remain Advanced.

## Company visual contract

`/company` groups systems semantically rather than rendering six identical cards:

- Intelligence: Company Brain.
- Operations: Meetings and Founder Approvals.
- Infrastructure: Models, Costs, and Agent Runs.

## Meeting visual contract

- Meeting execution architecture is unchanged.
- The giant ellipse table does not return.
- The page prioritizes title and compact status, participants, wide dialogue/contribution cards, Live Brief, cost/tokens/calls, and valid Founder controls.
- Completed Meeting Result becomes visually dominant.
- Speech cards must not become narrow essay columns.

## L — design system

- Page approximately `#070B10`, secondary `#0B1118`, elevated `#101821`.
- Primary text approximately `#F2F6FA`, secondary `#93A4B5`.
- Cyan is sparse and primarily marks CEO core, active/live state, selected navigation, and primary action.
- Headings are not universally cyan.

## M — card reduction

Primary composition uses purpose-specific structures:

`.command-stage`, `.ceo-core`, `.ceo-orbit`, `.command-input`, `.telemetry-strip`, `.executive-brief`, `.attention-signal`, `.operation-row`, `.event-stream`, `.org-chart`, `.org-branch`, `.employee-node`, and `.system-group`.

Generic `.surface`, `.panel`, and `.hero` may remain for secondary content only.

## N — motion

- Allowed motion is limited to CEO ambient orbit, online pulse, live Meeting indicator, and subtle hover transitions.
- Motion is CSS-only.
- Shared CSS contains `@media (prefers-reduced-motion: reduce)`.
- No complex motion.

## Executable behavioral contract

1. `/` redirects to `/command`.
2. Primary navigation contains exactly Command, Work, Team, Company; Models, Meetings, Costs, and Runs are not primary items.
3. `/command` contains `.command-stage`, `.ceo-core`, `.command-input`, `.telemetry-strip`, and `.executive-brief`.
4. CEO stage precedes executive brief, which precedes lower operations.
5. GET `/command` constructs or invokes zero providers.
6. `/team` renders organization hierarchy from real manager and department data.
7. `/company` exposes Intelligence, Operations, and Infrastructure groups.
8. Meeting retains wide structured contribution/result presentation without an ellipse-table structure.
9. Existing advanced and deep routes remain reachable.
10. Shared CSS contains reduced-motion treatment.

## Safety and authority

This patch changes presentation only. It adds no models, migrations, provider capabilities, execution semantics, governance changes, cost changes, autonomy, background workers, or frontend framework. Normal page loads remain zero-provider-call. No real provider call is authorized.

