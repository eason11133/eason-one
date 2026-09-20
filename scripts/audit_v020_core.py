from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
failures: list[str] = []


def text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def require(condition: bool, message: str) -> None:
    if not condition:
        failures.append(message)


runtime = text("eason_one/services/company_runtime.py")
kernel = text("eason_one/services/company_kernel.py")
outcome = text("eason_one/services/project_outcome.py")
contract = text("eason_one/services/project_contract.py")
boot = text("eason_one/__init__.py")
ops = text("eason_one/services/operations.py")
ceo = text("eason_one/services/ceo.py")
pyproject = text("pyproject.toml")
routes = text("eason_one/routes.py")
founder_decisions = text("eason_one/services/founder_decisions.py")
host_validation = text("eason_one/services/host_validation.py")
operation_kernel = text("eason_one/services/operation_kernel.py")
project_company = text("eason_one/services/project_company.py")
project_template = text("eason_one/templates/hq_project.html")
hq_js = text("eason_one/static/headquarters.js")
migrate = text("scripts/migrate_v020.py")

# Founder pause/resume/cancel controls are deliberately colocated with the
# process owner so every operator mutation wakes the same runtime.  Enforce a
# bounded shell plus absence of legacy business authority instead of a brittle
# pre-control line count.
require(len(runtime.splitlines()) < 500, "company_runtime.py is no longer a bounded process/control shell")
for forbidden in ("def _close_operation", "def _finalize_failed_operation", "def _persist_founder_result"):
    require(forbidden not in runtime, f"active runtime contains legacy business authority: {forbidden}")
require("legacy_company_runtime_v018" not in runtime.split("def migrate_latest_v017_candidate", 1)[0],
        "normal active runtime imports legacy runtime before explicit migration boundary")
require("company_kernel.advance_batch" in runtime, "runtime tick does not delegate to Company Kernel")
require('project.status = "FAILED"' not in kernel, "Company Kernel can directly fail a Project")
require('project.status = "FAILED"' not in outcome, "Project outcome module can directly fail a Project")
require("completion_criteria" not in kernel, "Company Kernel reads Mission completion criteria as Project authority")
require("completion_criteria" not in outcome, "Project outcome reads Mission completion criteria as Project authority")
require("Founder Project Contract" in contract and "contract_hash" in contract, "immutable Project Contract ledger is missing")
require("Founder Project Contract Amendment" in contract and "assert_authority_ledger" in contract,
        "append-only Founder Project authority amendment ledger is missing")
require("_HTTP_METHOD_PATH_RE = re.compile" in host_validation and "_HTTP_RE.search" not in host_validation,
        "host HTTP acceptance path can reference an undefined endpoint matcher")
require("authorize_budget_extension" in founder_decisions,
        "Founder Project budget extension does not route through Contract Amendment authority")
require("legacy_founder_project_cap_evidence" in contract and "reconcile_legacy_founder_project_cap" in contract,
        "v0.20 cannot safely recover explicit historical Founder Project budget authority")
require("reconcile_legacy_founder_project_cap" in migrate,
        "v0.20 migration does not reconcile provable historical Founder Project caps before freezing authority")
require("project_remaining_authority(project)" in project_company,
        "Founder Project surface computes authority remaining from total planning cost instead of execution authority")
require("execution authority left" in project_template.casefold(),
        "Founder Project budget label does not distinguish execution authority from Company planning cost")
require("durableGovernanceStates" in hq_js and "NEEDS_YOU" in hq_js,
        "transient runtime focus can overwrite durable Founder governance truth on Project pages")
require("operation.project.real_budget_limit =" not in founder_decisions,
        "Founder decision path directly mutates Project budget outside authority ledger")
require('project.status = "REVIEW"' in outcome, "Project Result Ready commit point is not owned by project_outcome")
require('project.status = "COMPLETED"' in outcome and "accept_founder_completion" in outcome,
        "explicit Founder completion is not committed through project_outcome")
require("current_evaluation = evaluate(project)" in outcome and "PROJECT_CONTRACT_NOT_SATISFIED" in outcome,
        "Project Result Ready trusts caller-supplied/stale evaluation instead of recomputing current Project truth")
require("project_outcome_basis_hash" in outcome and "current_basis_hash = outcome_basis_hash(project, current_evaluation)" in outcome,
        "Project Result proof is not bound to the current accepted evidence/source-lineage basis")
require("artifact_content_hash" in outcome and "_work_has_authoritative_acceptance(work)" in outcome,
        "Project host proof can outlive the exact accepted ArtifactVersion/content hash it verified")
require("PROJECT_RESULT_RECONCILIATION_STARTED" in kernel and 'project.status = "ACTIVE"' in kernel
        and "STALE_PROJECT_RESULT_PROOF" in kernel,
        "stale Result Ready proof is not automatically returned to Company-owned outcome reconciliation")
require('project.status = "COMPLETED"' not in routes,
        "Founder route can bypass Project-level result proof and directly complete a Project")
require("PROJECT_DELEGATED_CEO" in kernel and "PROJECT_DELEGATED_CEO" in ops,
        "bounded CEO continuation authority is not explicit end-to-end")
require(
    'status in {"COMPLETED", "FAILED"} and work.work_type == "MANAGEMENT"' in operation_kernel
    and "_assert_vnext_dispatch_state(operation, work)" in operation_kernel,
    "terminal Mission lifecycle still revokes vNext Project-management provider authority needed for outcome review/continuation",
)
require("SYSTEM_PRICED_BOUNDED_PROJECT_ENVELOPE" in ops,
        "default Project approval does not expose a deterministic bounded Project envelope")
require("continuation_reserve" in ops and "project_authorized_budget_twd" in ops,
        "Project budget compiler does not reserve bounded continuation authority")
require("reconcile_pending_proposal_authority(operation)" in ceo,
        "CEO proposal path does not compile one Founder-visible authority truth before approval")
require('version = "0.20.0"' in pyproject, "package version is not v0.20.0")
require("backfill_legacy_work_spine()" not in boot, "normal boot still replays historical Work migration")
require("import_legacy_v015_truth()" not in boot, "normal boot still replays v0.15 truth import")
require("migrate_latest_v017_candidate()" not in boot, "normal boot still performs historical runtime cutover")
require('"runtime_semantics": "WORK_CORE_V018"' in ops, "new Operations lost durable Work runtime semantics")
require('"core_rebuild_version": "0.20.0"' in ops, "new Operations are not stamped v0.20")
require("if _core_v018.is_v018_operation(operation):" in ops and "def reconcile_legacy_budget_event" in ops,
        "legacy precision reconciliation is not fenced away from v0.20 Project authority")

# Legacy Operations closure code may remain for non-vNext history, but next_step
# must fence WORK_CORE_V018 before any old business logic is considered.
tree = ast.parse(ops)
next_step = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "next_step"), None)
require(next_step is not None, "operations.next_step missing")
if next_step:
    src = ast.get_source_segment(ops, next_step) or ""
    prefix = src[:1800]
    require("core.is_v018_operation(operation)" in prefix and "COMPANY_RUNTIME_OWNS_EXECUTION" in prefix,
            "legacy operations.next_step is not fenced before old closure behavior")

if failures:
    print("v0.20 structural audit FAILED")
    for row in failures:
        print(" -", row)
    raise SystemExit(1)

# First usable Company trial: formal Employee identity is not a mock/provider binding.
execution_source = (ROOT / "eason_one" / "services" / "execution.py").read_text(encoding="utf-8")
policy_source = (ROOT / "eason_one" / "services" / "execution_policy.py").read_text(encoding="utf-8")
workforce_source = (ROOT / "eason_one" / "services" / "workforce.py").read_text(encoding="utf-8")
require("select_execution_model(employee, operation, purpose)" in execution_source, "central execution boundary must route formal Employee attempts")
require("formal and not _runtime_eligible(current)" in policy_source, "formal runtime must replace mock/unconfigured persistent bindings")
require("require_real=True" in workforce_source, "HR assessment pricing must use a configured real runtime provider")
print("v0.20 structural audit PASSED")
print(" - Founder Project Contract is the completion authority")
print(" - active runtime is a thin process shell")
print(" - legacy Mission closure/failure is outside the v0.20 governing path")
print(" - normal boot does not replay historical migrations")
print(" - Project continuation authority is explicit and bounded")


# V7: Perplexity Sonar structured output uses official json_schema; a provider
# HTTP client rejection is known/replayable only after deterministic repair.
providers_src = (ROOT / "eason_one" / "providers.py").read_text(encoding="utf-8")
execution_src = (ROOT / "eason_one" / "services" / "execution.py").read_text(encoding="utf-8")
effects_src = (ROOT / "eason_one" / "services" / "external_effects.py").read_text(encoding="utf-8")
recovery_src = (ROOT / "eason_one" / "services" / "runtime_recovery.py").read_text(encoding="utf-8")
assert '"type":"json_schema"' in providers_src and '"type":"json_object"' not in providers_src
assert "PROVIDER_REQUEST_REJECTED" in execution_src
assert "REJECTED_POST_DISPATCH" in effects_src
assert "reconcile_perplexity_response_format_rejections" in recovery_src

print(" - Founder budget expansion is append-only and authority-ledger validated")
