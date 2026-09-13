from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
failures: list[str] = []
passes: list[str] = []


def text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def require(condition: bool, message: str) -> None:
    (passes if condition else failures).append(message)


def function_source(path: str, name: str) -> str:
    source = text(path)
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    return ""


def imports_module(path: str, module_name: str) -> bool:
    tree = ast.parse(text(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == module_name for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom) and node.module == module_name:
            return True
    return False


governance = text("eason_one/services/governance.py")
contract = text("eason_one/services/project_contract.py")
runtime = text("eason_one/services/company_runtime.py")
kernel = text("eason_one/services/company_kernel.py")
escalations = text("eason_one/services/escalations.py")
founder = text("eason_one/services/founder_decisions.py")
work_execution = text("eason_one/services/work_execution.py")
codex = text("eason_one/services/codex_connector.py")
work_budget = text("eason_one/services/work_budget.py")
ops = text("eason_one/services/operations.py")
operation_kernel = text("eason_one/services/operation_kernel.py")
command = text("eason_one/services/command.py")
current_company = text("eason_one/services/current_company.py")
project_company = text("eason_one/services/project_company.py")
proposal = text("eason_one/services/proposal_authority.py")
workforce = text("eason_one/services/workforce.py")
routes = text("eason_one/routes.py")
outcome = text("eason_one/services/project_outcome.py")
migration = text("scripts/migrate_v020.py")
meeting_coordination = text("eason_one/services/meeting_coordination.py")
meeting_kernel = text("eason_one/services/meeting_kernel.py")
meetings = text("eason_one/services/meetings.py")
runtime_recovery = text("eason_one/services/runtime_recovery.py")
headquarters = text("eason_one/services/headquarters.py")
context = text("eason_one/services/context.py")
ceo = text("eason_one/services/ceo.py")
installer = text("scripts/install-v020.ps1")
run_headquarters = text("scripts/run-headquarters.ps1")
release_snapshot = text("eason_one/release_snapshot.py")
schemas = text("eason_one/schemas.py")
acceptance = text("eason_one/services/acceptance_contract.py")
host_validation = text("eason_one/services/host_validation.py")
work_runtime = text("eason_one/services/work_runtime.py")
text_normalization = text("eason_one/services/text_normalization.py")
execution = text("eason_one/services/execution.py")
task_execution = text("eason_one/services/task_execution.py")
reviews = text("eason_one/services/reviews.py")
market = text("eason_one/services/market.py")
legacy_tasks = text("eason_one/services/tasks.py")

expected_types = {
    "BUDGET_AUTHORIZATION",
    "PROJECT_DEADLINE_CHANGE",
    "PROJECT_SCOPE_CHANGE",
    "PROJECT_CONSTRAINT_CHANGE",
    "PROJECT_CANCEL",
    "CODEX_RISK_APPROVAL",
    "EXTERNAL_EFFECT_AUTHORIZATION",
}
module = ast.parse(governance)
founder_types = None
for node in module.body:
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "FOUNDER_ONLY_TYPES":
                founder_types = set(ast.literal_eval(node.value))
require(founder_types == expected_types, "Founder-only authority is an explicit seven-type whitelist")
require(not ({"PROJECT_AUTHORITY", "FOUNDER_AUTHORITY", "AUTHORITY_BLOCKED"} & (founder_types or set())),
        "Generic authority labels cannot become Founder authority types")

identity = function_source("eason_one/services/governance.py", "gate_identity")
require("options" in identity and "reason" not in identity.split("return", 1)[-1],
        "Gate identity is bound to exact frozen options, not wording")
require("_validate_gate(project, kind, options, work)" in governance,
        "Every canonical Founder gate passes centralized validation")
current_gate_src = function_source("eason_one/services/governance.py", "current_gate")
attention_src = function_source("eason_one/services/governance.py", "attention")
require("Escalation.id.asc()" in current_gate_src and "Escalation.id.asc()" in attention_src
        and "durable FIFO queue" in governance,
        "Founder authority preserves every unanswered question in a stable FIFO queue")
require("FOUNDER_GATE_NOT_CURRENT" in function_source("eason_one/services/governance.py", "resolve_gate")
        and "PROJECT_CANCELLED" in function_source("eason_one/services/governance.py", "resolve_gate"),
        "Queued authority cannot be answered out of order; explicit Project Cancel retires now-moot queued gates")
require("PROJECT_RESULT_READY_AUTHORITY_LOCKED" in governance and "PROJECT_ALREADY_COMPLETED" in governance,
        "Result Ready and terminal Project authority locks are centralized")
require("FOUNDER_BUDGET_GATE_REQUIRES_EXACT_POSITIVE_AMOUNT" in governance and '"scope": "PROJECT"' in governance,
        "Budget Founder gates require exact positive Project-scoped authority")
require("approval_receipt" in governance and "latest_execution_receipt" in governance and "requested_action_hash" in governance,
        "Execution Founder approval uses durable exact-action receipts")
require("governing_contract_hash" in governance and "budget_authority_hash" in governance,
        "Execution receipts are bound to current Contract and budget authority hashes")
require("Decision(" in governance and "authority_basis" in governance,
        "Canonical Founder decisions are first-class Decision truth")
resolve_gate = function_source("eason_one/services/governance.py", "resolve_gate")
require("FOUNDER_APPROVE_MUST_CONSUME_FROZEN_PAYLOAD" in resolve_gate,
        "APPROVE cannot override the exact payload frozen into the Founder gate")
require("FOUNDER_MODIFY_REQUIRES_EXACT_CHANGES" in resolve_gate and "_validate_gate(project, kind" in resolve_gate,
        "MODIFY requires exact changes and re-runs centralized authority validation")
require('if action == "APPROVE":' in resolve_gate and '_validate_gate(project, kind, _option_list(escalation.options_json), work)' in resolve_gate,
        "APPROVE revalidates current Project authority boundary at commit time")
require("resolved_authority_payload" in governance and "founder_changes" in governance and "applied_effects" in governance,
        "Founder Decision audit separates frozen proposal, resolved payload, requested changes, and applied effects")
require("def invalidate_gate" in governance and "FOUNDER_GATE_INVALIDATED" in governance,
        "Provably invalid Founder questions use non-authorizing canonical gate invalidation")

require(all(name in contract for name in ("has_frozen_contract", "is_vnext_governed", "governing_terms", "effective_authority")),
        "Project Contract exposes one validated governing read API")
require("PROJECT_CONTRACT_MISSING" in contract and "vNext may never silently fall back" in contract,
        "Governed Projects fail closed when Contract truth is missing")
require("def read_projection" in contract and "Governed Projects never fall back" in contract,
        "Founder/read-only Contract projection fault-contains integrity errors without weakening vNext authority")
require("read_projection" in project_company and "contract_integrity_error" in project_company,
        "Headquarters Project cards fault-contain Contract integrity failures instead of crashing the whole Founder surface")
require("read_projection" in headquarters and "authority_integrity_error" in headquarters,
        "Mission Founder snapshot fault-contains Contract integrity failures without projecting legacy budget authority")
require("governing_terms" in context and "read_projection" in ceo,
        "Paid execution fails closed on Contract truth while broad CEO reads fault-contain unrelated integrity failures")
require(all(name in contract for name in ("authorize_budget_extension", "authorize_deadline_change", "authorize_scope_change", "authorize_constraint_change")),
        "Project authority changes are append-only Contract amendments")
require("PROJECT_RESULT_READY_AUTHORITY_LOCKED" in contract or "_assert_amendable" in contract,
        "Project Contract amendment layer enforces a Result Ready/terminal boundary")
require("assert_authority_ledger" in work_budget and "is_vnext_governed" in work_budget,
        "Work budget reader derives authority from validated Project Contract")
require("effective_authority" in ops and "project_remaining_authority" in ops,
        "Mission planning budget reader uses Project authority ledger")
proposal_snapshot = function_source("eason_one/services/operations.py", "proposal_authority_snapshot")
require("assert_authority_ledger" in proposal_snapshot and "is_vnext_governed" in proposal_snapshot,
        "Existing-Project proposal budget projection validates the Founder Contract ledger")
public_snapshot = function_source("eason_one/services/operation_kernel.py", "public_snapshot")
require("assert_authority_ledger" in public_snapshot and "is_vnext_governed" in public_snapshot,
        "Mission public budget snapshot validates the Founder Contract ledger")

resume = function_source("eason_one/services/company_runtime.py", "resume_after_founder")
require(
    'resolve_waits(\n            work, "FOUNDER_PAUSE"' in resume
    and 'resolve_waits(work, "BUDGET"' not in resume
    and 'resolve_waits(work, "RECONCILIATION"' not in resume
    and '.state = "RESOLVED"' not in resume,
    "Manual Resume can clear only the manual pause control wait",
)
cancel = function_source("eason_one/services/company_runtime.py", "cancel_operation")
require("governance.is_founder_type" in cancel and "MISSION_CANCELLED" in cancel,
        "Mission Stop cannot resolve Project Founder authority")
require("V020_FOUNDER_AUTHORITY_MUST_USE_CANONICAL_GOVERNANCE" in escalations,
        "Generic Escalation writer cannot mint vNext Founder-only gates")
require("no current canonical Founder Governance gate" in founder,
        "Approved governed Mission cannot use legacy Founder decision path")
require("Runtime did not identify a precise Founder-only authority type" in kernel,
        "Generic AUTHORITY_BLOCKED stays Company reconciliation")
require("Exact positive Project budget shortfall was not proven" in kernel,
        "Kernel refuses blank/guessed Founder budget gates")
require(not imports_module("eason_one/services/company_kernel.py", "eason_one.services.legacy_company_runtime_v018"),
        "Normal Company Kernel does not import legacy v0.18 business runtime")
require(not imports_module("eason_one/services/runtime_recovery.py", "eason_one.services.legacy_company_runtime_v018"),
        "Current platform recovery does not import legacy v0.18 business runtime")
require("invalidate_gate" in runtime_recovery and "CODEX_RISK_APPROVAL" in runtime_recovery,
        "Platform fault recovery invalidates obsolete Codex gates without Founder authorization")
require('escalation.state = "RESOLVED"' not in contract,
        "Project Contract compatibility reconciliation cannot directly resolve Founder gates")

require("FOUNDER_APPROVAL_REQUIRES_FRESH_EXECUTION" in work_execution,
        "Pre-approval Codex AgentRun cannot be reused after Founder approval")
require("APPROVED_EXCEPTION_NOT_CONSUMED" in work_execution and "approval_receipt" in work_execution,
        "Repeated exact Codex Founder request becomes reconciliation, not a duplicate gate")
require("latest_execution_receipt" in codex and "still-valid Founder-approved exception" in codex,
        "Fresh Codex Job Spec consumes the still-valid exact Founder exception")

require("def is_initial_project_proposal" in proposal and "return False" in function_source("eason_one/services/proposal_authority.py", "is_initial_project_proposal"),
        "Historical PROJECT_PLAN proposals cannot become current Founder authority")
require("is_initial_project_proposal" in command and "governance.attention" in command,
        "Command Center Founder attention excludes retired PROJECT_PLAN proposals and generic pending work")
require("is_initial_project_proposal" in current_company and "governance.attention" in current_company,
        "HQ Founder attention reads canonical Governance and excludes retired PROJECT_PLAN proposals")
require("governance.attention" in project_company,
        "Project Founder surface reads canonical Governance attention")

synthesis = function_source("eason_one/services/meetings.py", "end_and_synthesize")
router_apply = function_source("eason_one/services/meetings.py", "_apply_router_result")
require('legacy_status="WAITING_FOR_FOUNDER"' not in synthesis and "_handle_step_failure" in synthesis
        and all('legacy_status="PAUSED"' in function_source("eason_one/services/meetings.py", name)
                for name in ("_fail_step", "_paid_fail_step")),
        "Meeting synthesis/provider/validation failures project to Company recovery, not Founder attention")
require('MEETING_FOUNDER_INPUT_REQUIRED' in router_apply and 'legacy_status="WAITING_FOR_FOUNDER"' in router_apply,
        "Only an explicit router Founder question creates the Meeting Founder-wait projection")
require('"FAILED": "FAILED"' in meeting_kernel and '"FAILED": "WAITING_FOR_FOUNDER"' not in meeting_kernel,
        "Terminal Meeting failure cannot masquerade as Founder input")
require("requires_founder_input" in meeting_coordination and "MEETING_FOUNDER_INPUT_REQUIRED" in meeting_coordination,
        "Meeting Founder attention requires canonical question evidence, not a legacy status string")
require("requires_founder_input" in command and 'status="WAITING_FOR_FOUNDER"' in command,
        "Command Center filters Meeting Founder attention through the canonical conversational-input classifier")
require("Bounded Company recovery is pending" in meetings and "Founder action required" not in meetings,
        "Paid Meeting provider failures are described as bounded Company recovery, not Founder authority")

require("DELEGATED_HIRING_AUTHORITY_V1" in workforce and '"new_budget_authority_twd": "0"' in workforce,
        "Delegated hiring grants organizational capacity but zero new budget authority")
require("DELEGATED_HIRE_COMMITTED" in workforce and "Decision(" in workforce,
        "Delegated hiring persists Company Decision + Event truth")
review = function_source("eason_one/services/workforce.py", "review_request")
approve_hire = function_source("eason_one/services/workforce.py", "approve_hire")
require("commit_delegated_hire" in review and "_delegated_hiring_authority" in approve_hire,
        "Provider and manual HR review paths share delegated hiring authority semantics")

require(routes.count("request_budget_gate(") >= 2 and routes.count("resolve_gate(") >= 3,
        "Both Founder budget HTTP writers route governed Projects through canonical Governance")
require("headquarters_project_cancel" in routes and 'escalation_type="PROJECT_CANCEL"' in routes,
        "Entire Project Cancel has an explicit Project-level Governance route")
require("accept_founder_completion" in routes and "accept_founder_completion" in outcome,
        "Founder Result acceptance is a Project-level verified outcome decision")
require("PROJECT_RESULT_ACCEPTANCE" in outcome and "Decision(" in outcome,
        "Founder Result acceptance persists first-class Decision provenance")
close_ready = function_source("eason_one/services/project_outcome.py", "close_result_ready")
require('operation.status = "COMPLETED"' not in close_ready and 'operation.kernel_status = "COMPLETED"' not in close_ready,
        "Project Result Ready cannot rewrite historical Mission lifecycle outcomes")

require("build_legacy_contract" in migration and "has_frozen_contract" in migration,
        "Explicit v0.20 migration can freeze historical Founder evidence without weakening runtime fail-closed reads")
require("get_contract(project)\n                if has_frozen_contract(project)" in migration,
        "Migration uses historical synthesis only before first immutable freeze")

require("Assert-InstalledTreeMatchesSource" in installer and "Get-ApplicationManifest" in installer
        and "HASH mismatch" in installer and "MISSING target file" in installer,
        "Installer refuses to continue unless application-owned target bytes match package source")
require("Get-RelativePathCompat" in installer and "GetRelativePath(" not in installer,
        "Installer manifest verification is compatible with Windows PowerShell 5.1 / .NET Framework")
require('$ErrorActionPreference = "Stop"' in installer and "catch {" in installer and "Restore-Backup" in installer and "throw" in installer,
        "Installer failures abort the installer and preserve rollback semantics")
require("requests_governed_work" in operation_kernel and '"新增"' in operation_kernel,
    "Founder work intent routing shares governed-work classification and recognizes Chinese add/create requests")
require("ENGINEERING_SNAPSHOT_ID" in run_headquarters and "project_company.py SHA256" in run_headquarters
        and "architecture-reliability-20260902" in release_snapshot,
        "Headquarters startup exposes the engineering snapshot identity and critical source fingerprint")
require("AUTHORITATIVE HOST PROOF FOR THIS EXACT ARTIFACT VERSION" in work_execution
        and "WORK_REVIEW_V4_ARTIFACT_SOURCE_LINEAGE" in work_execution
        and "must not override a later PASSED host VerificationRecord" in work_execution,
        "Independent semantic review receives exact ArtifactVersion-bound host proof instead of trusting stale WSL limitations")
require("HOST_PROOF_REVIEW_HANDOFF_V2" in kernel
        and "PROJECT_SYSTEM_RECOVERY_PROTOCOL_REPAIRED" in kernel
        and "system_recovery_protocol_override" in kernel,
        "Repeated-failure SYSTEM_RECOVERY can resume exactly once after the evidence-handoff protocol is repaired")
require("FounderAuthorityPreviouslyRejected" in governance and "_current_rejection" in governance
        and "FOUNDER_GATE_REOPEN_SUPPRESSED" in governance,
        "Founder REJECT is durable negative authority and suppresses automated gate reopening")
require('if kind == "BUDGET_AUTHORIZATION":\n            return decision' in governance
        and "budget_authority_hash" in governance and "governing_contract_hash" in governance,
        "Rejected Project budget cap remains binding while governing Contract/authority are unchanged")
require("allow_founder_reconsideration" in governance and "allow_founder_reconsideration=True" in routes,
        "Explicit Founder-initiated authority change remains possible without letting Company nag after rejection")
require("AUTHORITY_EXHAUSTED" in kernel and "_reconcile_authority_exhausted_outcome" in kernel
        and "paid_continuation_started" in kernel,
        "Rejected Project budget stops paid continuation while allowing zero-cost evidence reconciliation")
require("reconcile_reopened_rejected_founder_gates" in runtime_recovery
        and "FOUNDER_REJECT_NEGATIVE_AUTHORITY_RECONCILED" in runtime_recovery
        and "rejected_decision_for_gate" in runtime_recovery,
        "Restart/upgrade invalidates already-open duplicate budget gates created after an earlier Founder REJECT")

require('"maxItems": 8' in schemas and '"maxLength": 280' in schemas,
        "Founder/Work acceptance schemas preserve up to eight bounded 280-character criteria instead of truncating authority")
require("OBJECT REPLACEMENT CHARACTER" in text_normalization and "REPLACEMENT CHARACTER" in text_normalization and "clean_rows" in text_normalization,
        "Provider transport/control garbage is normalized by one shared text boundary before immutable truth")
require("_sanitize_operation_plan" in ops and "clean_rows" in contract and "_sanitize_options" in governance,
        "Project plan, Contract and explicit Governance-option writers share sanitation before freeze/hash")
require("_http_acceptance_checks" in host_validation and "expected_status" in host_validation
        and "require_current_version" in host_validation and "require_health_check" in host_validation,
        "Host verification proves each frozen endpoint contract with exact status, version source and health preservation")
require("HOST_HTTP_CONTRACT" in acceptance and "one unambiguous primary" in acceptance,
        "Response-only HTTP evidence binds only to one unambiguous frozen endpoint instead of guessing")
require("WORK_REVIEW_V4_ARTIFACT_SOURCE_LINEAGE" in work_execution and "RECONCILIATION" in work_execution
        and "UNPROVEN is an evidence state, not an implementation failure" in work_execution,
        "UNPROVEN semantic review preserves host-valid implementation and reconciles evidence instead of rerunning code")
require("criterion_id" in outcome and "PROJECT_OUTCOME_REVIEW_V1" in kernel
        and "P1..P8" in kernel,
        "Project outcome review addresses stable contract-bound criterion ids rather than model-repeated criterion text")
close_ready = function_source("eason_one/services/project_outcome.py", "close_result_ready")
require("PROJECT_RESULT_READY_BLOCKED_BY_FOUNDER_AUTHORITY" in close_ready
        and "PROJECT_RESULT_READY_BLOCKED_BY_ACTIVE_WORK" in close_ready,
        "Result Ready refuses to race unresolved Founder authority or active delivery Work")
advance_project = function_source("eason_one/services/company_kernel.py", "_advance_project")
require("governance.attention(project)" in advance_project
        and 'Escalation.query.filter_by(project_id=project.id, state="OPEN").count()' not in advance_project,
        "Generic internal Escalation rows cannot deadlock whole-Project advancement")
require("def blocks_work" in governance and "governance.blocks_work(work)" in kernel,
        "Canonical Founder authority centrally blocks lawful Work execution independent of UI wait projections")
require("def reopen_abandoned" in work_runtime and "WORK_SYSTEM_REOPENED" in work_runtime
        and "reopen_abandoned" in runtime_recovery
        and 'work.state = "READY"' not in function_source("eason_one/services/runtime_recovery.py", "reconcile_validation_scope_faults"),
        "Platform repair reopens terminal ABANDONED Work only through an explicit auditable recovery exception")
briefing = function_source("eason_one/services/ceo.py", "generate_project_briefing")
require("is_vnext_governed" in briefing and (
        "governing_terms" in briefing or "GOVERNED_PROJECT_BRIEFING_REQUIRES_WORK_AUTHORITY" in briefing),
        "Paid CEO Project synthesis either reads immutable Contract truth or rejects execution without governed Work authority")
canonical_gate = function_source("eason_one/services/founder_decisions.py", "_canonical_vnext_gate")
require("current = governance.current_gate(project)" in canonical_gate and "int(escalation_id) != current.id" in canonical_gate,
        "Legacy Mission Founder controls cannot bypass FIFO Project authority with a stale queued gate projection")

adopt = function_source("eason_one/services/company_kernel.py", "adopt_operation")
adopt_all = function_source("eason_one/services/company_kernel.py", "adopt_approved_operations")
require('operation.project.status = "ACTIVE"' not in adopt
        and 'operation.project.status == "ACTIVE"' in adopt
        and "any_work_gate" in adopt and "governance.attention(operation.project)" in adopt,
        "Restart adoption preserves durable BLOCKED/REVIEW Project lifecycle and cannot clear authority/recovery gates")
require("PROJECT_ADOPTION_FAILED_CLOSED" in adopt_all and 'project.status = "BLOCKED"' in adopt_all,
        "Startup adoption failures become visible Company recovery truth instead of silent scheduler skips")

require("PROJECT_HARD_WAIT_TYPES" in work_runtime and "def project_hard_blockers" in work_runtime
        and "def project_can_activate" in work_runtime,
        "Project ACTIVE writes share one whole-Project hard-block predicate instead of checking only a local wait")
terminal_activate = function_source("eason_one/services/work_runtime.py", "project_can_activate")
terminal_recovery = function_source("eason_one/services/runtime_recovery.py", "_is_kernel_work")
terminal_execute = function_source("eason_one/services/execution.py", "execute")
terminal_task_execute = function_source("eason_one/services/task_execution.py", "run_task")
terminal_task_review = function_source("eason_one/services/reviews.py", "run_review")
terminal_kernel = function_source("eason_one/services/company_kernel.py", "_advance_project")
require(
    'TERMINAL_PROJECT_STATES = {"COMPLETED", "CANCELLED"}' in work_runtime
    and "project_execution_fenced(project)" in terminal_activate
    and "project_execution_fenced(project)" in terminal_recovery
    and "PROJECT_TERMINAL_EXECUTION_FORBIDDEN" in terminal_execute
    and "PROJECT_TERMINAL_TASK_EXECUTION_FORBIDDEN" in terminal_task_execute
    and "PROJECT_TERMINAL_TASK_REVIEW_FORBIDDEN" in terminal_task_review
    and "PROJECT_TERMINAL_OPERATION_TASK_FORBIDDEN" in ops
    and "PROJECT_TERMINAL_OPERATION_REVIEW_FORBIDDEN" in ops
    and "PROJECT_TERMINAL_OPERATION_MEETING_FORBIDDEN" in ops
    and "PROJECT_TERMINAL_CODEX_EXECUTION_FORBIDDEN" in codex
    and "PROJECT_TERMINAL_HIRING_REQUEST_FORBIDDEN" in workforce
    and "PROJECT_TERMINAL_HIRING_MUTATION_FORBIDDEN" in workforce
    and "PROJECT_TERMINAL_MEETING_CREATION_FORBIDDEN" in meetings
    and "PROJECT_TERMINAL_MEETING_MUTATION_FORBIDDEN" in meetings
    and "PROJECT_TERMINAL_OPERATION_NEXT_STEP_FORBIDDEN" in ops
    and "TERMINAL_RECONCILED" in ops
    and "provider_replayed" in ops
    and "domain_materialized" in ops
    and "PROJECT_TERMINAL_MARKET_AWARD_FORBIDDEN" in market
    and "PROJECT_TERMINAL_MARKET_ROTATION_FORBIDDEN" in market
    and "PROJECT_TERMINAL_MARKET_REISSUE_FORBIDDEN" in market
    and "PROJECT_TERMINAL_ESCALATION_CREATION_FORBIDDEN" in escalations
    and "PROJECT_TERMINAL_LEGACY_TASK_WRITER_FORBIDDEN" in legacy_tasks
    and 'if project.status in PROJECT_TERMINAL or project.status == "PAUSED"' in terminal_kernel,
    "Terminal Project lifecycle is a hard operational fence across recovery, provider, Work, HR, Meeting, market and legacy side doors",
)
terminal_truth = function_source("eason_one/services/company_truth.py", "project_snapshot")
terminal_employee_truth = function_source("eason_one/services/company_truth.py", "employee_activity")
terminal_runtime_snapshot = function_source("eason_one/services/company_runtime.py", "runtime_snapshot")
terminal_workload = function_source("eason_one/services/team_formation.py", "_active_workload")
terminal_project_card = function_source("eason_one/services/project_company.py", "project_card")
terminal_work_command = function_source("eason_one/services/project_company.py", "work_command_snapshot")
terminal_global_runtime = function_source("eason_one/services/stabilization.py", "global_runtime_snapshot")
terminal_active_run = function_source("eason_one/services/stabilization.py", "active_execution_run")
terminal_hq_live = function_source("eason_one/services/headquarters.py", "operation_live_snapshot")
require(
    '{"COMPLETED", "CANCELLED", "FAILED"}' in current_gate_src
    and 'historical_terminal_residuals' in terminal_truth
    and 'open_waits = []' in terminal_truth
    and '{"PAUSED", "COMPLETED", "CANCELLED", "FAILED"}' in terminal_employee_truth
    and 'project_terminal' in terminal_runtime_snapshot and 'active_runs = [] if project_terminal' in terminal_runtime_snapshot
    and 'Project.status.in_(["PLANNING", "ACTIVE", "BLOCKED", "REVIEW"])' in terminal_workload
    and 'Project.status.in_(OPERATING_PROJECT_STATUSES)' in current_company
    and 'Project.status.in_(ACTIVE_PROJECT)' in command
    and 'project_terminal' in terminal_hq_live and 'meeting and not project_terminal' in terminal_hq_live
    and 'terminal_projection' in terminal_project_card and 'attention = []' in terminal_project_card
    and 'Project.status.notin_(PROJECT_TERMINAL)' in terminal_work_command
    and 'Project.status.notin_(["COMPLETED", "FAILED", "CANCELLED", "PAUSED"])' in terminal_global_runtime
    and 'Project.status.notin_(["COMPLETED", "FAILED", "CANCELLED", "PAUSED"])' in terminal_active_run,
    "Terminal Project history is sanitized out of Founder attention, employee capacity, live runtime/dashboard activity and staffing workload without deleting audit rows",
)
terminal_portfolio = function_source("eason_one/services/ceo_management.py", "portfolio_review")
terminal_company_plan = function_source("eason_one/services/ceo_management.py", "company_plan")
terminal_focus = function_source("eason_one/services/stabilization.py", "choose_focus")
terminal_meeting_view = function_source("eason_one/services/headquarters.py", "_meeting_view")
terminal_employee_presence = function_source("eason_one/services/headquarters.py", "employee_presence")
terminal_meetings_snapshot = function_source("eason_one/services/headquarters.py", "meetings_snapshot")
terminal_ceo_scope = function_source("eason_one/services/ceo.py", "_select_real_scope")
require(
    'historical_terminal_project_ids' in terminal_portfolio
    and 'terminal_history_has_operating_effect' in terminal_portfolio
    and '!= "TERMINAL"' in terminal_portfolio
    and 'terminal_history_has_operating_effect' in terminal_company_plan
    and '!= "TERMINAL"' in terminal_company_plan
    and 'current_rows' in terminal_focus and 'classification") != "TERMINAL"' in terminal_focus
    and 'terminal_project' in terminal_meeting_view and 'TERMINAL_PROJECT_STATUSES' in terminal_meeting_view
    and '~Project.status.in_(tuple(TERMINAL_PROJECT_STATUSES))' in terminal_employee_presence
    and 'TERMINAL_PROJECT_STATUSES' in terminal_meetings_snapshot
    and 'current_projects' in terminal_ceo_scope
    and 'row.project is None' in terminal_ceo_scope
    and '~Project.status.in_(["COMPLETED","FAILED","CANCELLED"])' in routes,
    "Terminal Project history cannot re-enter current Portfolio/CompanyPlan priority, HQ focus, live Meeting presence, CEO auto-scope or new-Meeting selection",
)

eligible_work = function_source("eason_one/services/company_kernel.py", "_eligible_works")
require("project_hard_blockers(work.project, include_founder=False)" in eligible_work,
        "A hard blocker on management/sibling Work freezes every runnable Work in that Project instead of allowing side-channel spending")
require("project_can_activate(project)" in kernel
        and "project_can_activate(operation.project)" in runtime
        and "project_can_activate(project)" in runtime_recovery
        and "project_can_activate(project)" in outcome,
        "Kernel, operator controls, restart recovery and outcome sequencing cannot revive Project ACTIVE across another hard blocker")

mission_failed = function_source("eason_one/services/company_kernel.py", "_mark_mission_failed")
plan_continuation = function_source("eason_one/services/company_kernel.py", "_plan_continuation")
require("EVIDENCE_REVIEW_EXHAUSTED" in mission_failed and 'failure_mode = "EVIDENCE_ONLY"' in mission_failed
        and "continuation_failure_mode" in mission_failed,
        "Evidence-review exhaustion is durable Project continuation context instead of being flattened into generic implementation failure")
require("CONTINUATION MODE IS EVIDENCE_ONLY" in plan_continuation
        and "READ-ONLY EVIDENCE RECONCILIATION" in plan_continuation
        and 'memory["continuation_mode"]' in plan_continuation,
        "Evidence-only continuation is authority-narrowed so the same implementation cannot be replayed merely to obtain proof")

require("CODEX_EXECUTION_BOUNDARY_V2" in codex and "CODEX_WRITE_SCOPE_V1" in codex
        and "def ensure_execution_boundary" in codex and "def _create_isolated_workspace" in codex
        and "def _copy_back_approved_paths" in codex
        and "boundary_hash" in codex and "project_execution_terms_hash" in codex,
        "Codex repository/tool scope is frozen and hash-bound on Work before execution/retry")
require("ensure_execution_boundary(work, task)" in work_runtime
        and 'engineering.get("allowed_paths")' not in function_source("eason_one/services/codex_connector.py", "build_job_spec").split("if boundary:", 1)[0],
        "vNext Work materialization freezes Codex boundary before Job Spec generation instead of trusting later mutable Operation memory")

legacy_materializer = function_source("eason_one/services/operations.py", "_materialize_task_success")
require("VNEXT_LEGACY_TASK_MATERIALIZER_FORBIDDEN" in legacy_materializer,
        "Legacy Task success materialization cannot become an alternate vNext Artifact/Founder-authority writer")

require("HOST_PROOF_RETRY" in work_execution and "HOST_PROOF_RETRY" in runtime_recovery
        and 'open_wait(work, "VERIFICATION"' not in work_execution,
        "Deterministic proof gaps have an owned bounded retry/reconciliation path instead of immortal VERIFICATION waits")
require("VNEXT_WAIT_OWNERS" in work_runtime and "VNEXT_WAIT_TYPE_HAS_NO_OWNER" in work_runtime
        and '"BUDGET"' not in function_source("eason_one/services/work_runtime.py", "open_wait")
        and '"VERIFICATION"' not in function_source("eason_one/services/work_runtime.py", "open_wait"),
        "Every vNext Work wait has a named owner/exit policy and unknown legacy wait labels fail closed")
rejected_boundary = function_source("eason_one/services/governance.py", "apply_rejected_budget_boundary")
require("ensure_management_work" in rejected_boundary and "AUTHORITY_EXHAUSTED" in rejected_boundary,
        "Founder budget rejection always leaves Project management a durable AUTHORITY_EXHAUSTED owner even when the rejected gate came from delivery Work")

if failures:
    print(f"v0.20 Governance structural audit FAILED ({len(passes)} pass / {len(failures)} fail)")
    for row in failures:
        print(" - FAIL:", row)
    raise SystemExit(1)

print(f"v0.20 Governance structural audit PASSED ({len(passes)} invariants)")
for row in passes:
    print(" - PASS:", row)
