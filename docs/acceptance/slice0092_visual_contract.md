# Patch 009.2 Founder Visual Acceptance Contract

This contract is frozen before Patch 009.2 product implementation. It records the Founder’s screenshot findings and the structural evidence required from the correction. Automated checks do not establish subjective visual acceptance.

## Scope

This is a frontend-only correction limited to Command spatial composition, Team personnel and reporting presence, active Meeting composition, terminal result-first Meeting presentation, and the shared Dark Depth System.

The patch must not change database models, migrations, CEO or Meeting execution, validation or recovery semantics, providers, costs, Brain or Proposal governance, autonomy, background execution, or frontend framework. Company is already accepted and receives only shared depth tokens. Work retains its Patch 009.1 operations-board composition. Normal navigation remains zero-provider-call, and no real provider call is authorized.

## A — Four-level Dark Depth System

Shared CSS defines four perceptibly distinct dark tokens:

1. Canvas.
2. Primary surface.
3. Raised surface.
4. Focused or interactive surface.

It also defines shared strong and soft cool-toned borders, primary text, secondary text, muted text, and sparse cyan accent. Major regions must not collapse into an indistinguishable nearly-black canvas. Depth must come from shared tokens rather than arbitrary page-specific black values. The approved dark blue/black direction, white typography, and cyan accent remain.

## B — Command first viewport

Keep the accepted orbital CEO core, telemetry, greeting, central CEO presence, integrated composer, and executive briefing concept.

At a typical desktop viewport:

- The CEO remains the dominant identity and is not enlarged.
- The stage is materially shorter than Patch 009.1.
- Dead space between greeting, CEO, composer, and Brief is reduced.
- The Brief transitions upward from the stage.
- The beginning of active Company state appears without a full viewport scroll.
- Layout is responsive, not hard-coded to one viewport height.
- Primary navigation remains visibly usable at desktop widths.
- Atmosphere may use only very soft illumination, tonal gradients, and nearly invisible depth lines.

DOM order remains Command Stage, Executive Brief, then lower Company operations and activity.

## C — Personnel nodes

Every active Employee on `/team` renders as a compact `.personnel-node` containing:

- A dedicated `.employee-avatar` person visual, not only a letter in a generic circle.
- Name and role hierarchy.
- Visible deterministic status that does not depend on color alone.
- Model or level metadata only as secondary information.

Nodes must read as people in an organization rather than administrative settings cards. No remote images or realistic portraits are used; CSS, inline SVG, or lightweight local primitives are allowed.

## D — Real reporting tree

The organization is deterministically built from `Employee.manager_id`, Department, and Position.

If Manager A reports to CEO and Worker A reports to Manager A, Worker A must be structurally nested beneath Manager A in the rendered tree. It is insufficient to render Department members as siblings with only `data-manager-id`.

CEO is the root. Departments provide subtle spatial zones, while actual manager relationships determine nesting and reporting lines. No Employee names are hard-coded.

## E — Active Meeting space usage

For a non-terminal Meeting, the primary desktop region is a responsive two-column composition:

- Approximately 65% Meeting Room.
- Approximately 35% Live Brief.

It uses the available container width, has no unused third column, contains no absolute/fixed seat layout, and uses `minmax` grid or flex patterns. Participant contributions must not remain narrow fixed-width essay cards.

## F — Dialogue-first Meeting

The active Founder view presents participants as Employees having a discussion:

- Shared Employee avatar language.
- Participant identity and role/model secondary.
- Natural-language dialogue body primary.
- Relation indicator such as QUALIFY, DISAGREE, or ADD where present.
- Compact action/control and risk counts.

Structured POSITION, ACTIONS, RISK, CONTROLS, EVIDENCE, and validation warnings remain available under collapsed per-contribution details. The default view must not force the Founder to read structured schema fields.

The Live Brief is a raised right-side operational summary prioritizing current topic, agreement, qualification/disagreement, key risk, next step, and compact cost/token/call metrics. Empty or low-information sections are omitted when safe.

## G — Terminal result-first mode

For `ENDED`, or `TERMINATED_BY_FOUNDER` with a recovered result:

- Meeting title and compact status appear first.
- `.result-mode` and Meeting Result precede `.discussion-details`, Live Brief, structured contribution details, and audit.
- Discussion defaults collapsed or clearly secondary.
- Founder does not scroll through the full discussion to reach the Result.
- No large empty Founder Control block renders when no valid action exists.

Result surfaces use internal tonal hierarchy: result surface, decision/focus area, qualification/risk shifts, and compact raised metrics. Typography and surface shifts take precedence over excessive bordered cards.

## H — Recovery truth

A recovered terminated Meeting explicitly retains:

- Original status: `TERMINATED_BY_FOUNDER`.
- Recovery: `LOCAL · 0 API CALLS`.

Presentation must not imply normal completion. Recovery and Meeting execution semantics remain unchanged.

## I — Responsive and accessible presentation

- Desktop is primary.
- Meeting stacks Room then Live Brief below the appropriate width.
- Team becomes a nested personnel list on narrow screens.
- Command retains the CEO and usable composer without horizontal overflow.
- Reduced-motion behavior remains.
- Avatar and status meaning does not depend on color alone.
- Secondary text remains readable.
- No large contentless region resembles unfinished or unloaded UI.

## Frozen executable scenarios

1. Shared CSS defines distinct canvas, surface, raised, and focused depth tokens.
2. Command Stage precedes Executive Brief, which precedes lower Company operations.
3. Primary navigation remains exactly Command, Work, Team, Company.
4. Team Employee nodes contain dedicated person/avatar visuals.
5. A worker is structurally nested beneath the Employee identified by its `manager_id`.
6. Active Meeting renders a two-column Meeting Room and Live Brief structure without a third blank column.
7. Active contribution renders human-readable dialogue first, with structured fields inside collapsed details.
8. Terminal Meeting Result precedes Discussion in DOM order.
9. Terminal Discussion is collapsed or secondary by default.
10. A terminal Meeting without valid actions renders no empty Founder Control block.
11. Recovered Meeting displays terminated original status and local zero-call recovery truth.
12. GET `/command`, GET `/team`, and GET terminal Meeting construct or invoke zero providers.
13. Existing Meeting recovery and two-person Economy execution regressions remain unchanged and green.

## Visual authority

Automated tests validate structure and safety, not beauty. Codex must report implementation and automated-contract status only. The Founder decides screenshot acceptance after reviewing real rendered screenshots.

