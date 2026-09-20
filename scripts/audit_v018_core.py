"""Dependency-free structural audit for the v0.18 Core Cutover.

This is an engineering guardrail, not product acceptance. Real acceptance still
requires an actual Founder-approved Project in the live Windows environment.
"""
from __future__ import annotations

import ast
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]


def text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def function_source(rel: str, name: str) -> str:
    source = text(rel)
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"Missing function {name} in {rel}")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    pyproject = text("pyproject.toml")
    core = text("eason_one/services/core_v018.py")
    company = text("eason_one/services/company_runtime.py")
    work_runtime = text("eason_one/services/work_runtime.py")
    operations = text("eason_one/services/operations.py")
    stabilization = text("eason_one/services/stabilization.py")
    project_company = text("eason_one/services/project_company.py")
    company_truth = text("eason_one/services/company_truth.py")
    global_runtime = text("eason_one/static/global-runtime.js")
    headquarters_js = text("eason_one/static/headquarters.js")

    require('version = "0.18.0"' in pyproject, "Package version is not 0.18.0")
    require('RUNTIME_SEMANTICS = "WORK_CORE_V018"' in core, "Missing explicit v0.18 runtime marker")
    require('memory["runtime_semantics"] = RUNTIME_SEMANTICS' in core, "Cutover stamp does not persist runtime semantics")

    eligible = function_source("eason_one/services/company_runtime.py", "_eligible_work")
    require("Task" not in eligible and "task.status" not in eligible, "Task still governs v0.18 scheduler eligibility")
    require("WaitCondition" not in eligible, "WaitCondition still governs v0.18 scheduler eligibility")
    require("is_work_vnext(operation)" in eligible, "Scheduler is not restricted to the v0.18 lane")

    loop = function_source("eason_one/services/company_runtime.py", "_loop")
    require("migrate_latest_v017_candidate" not in loop, "Restart loop can migrate another historical Project")
    require("operation_kernel" not in loop, "Legacy OperationKernel is reachable from v0.18 scheduler loop")
    require("_repair_legacy_state_waits" not in loop and "_repair_unbounded_internal_waits" not in loop,
            "Legacy WaitCondition recovery remains active")

    migrate = function_source("eason_one/services/company_runtime.py", "migrate_latest_v017_candidate")
    require("already_cut_over" in migrate and "RUNTIME_SEMANTICS" in migrate,
            "Safe migration is not guarded as a one-time cutover")
    require(company.count("WaitCondition.query") == 1,
            "Company Runtime has WaitCondition queries outside the one-time migration adapter")

    open_wait = function_source("eason_one/services/work_runtime.py", "open_wait")
    resolve_waits = function_source("eason_one/services/work_runtime.py", "resolve_waits")
    require("runtime_control_json" in open_wait or 'data["gates"]' in open_wait,
            "v0.18 gates are not stored on Work")
    require("if _is_v018_work(work)" in open_wait and "if _is_v018_work(work)" in resolve_waits,
            "Work-owned v0.18 gate branch is missing")

    for name in ("resume", "pause", "stop"):
        fn = function_source("eason_one/services/operations.py", name)
        require("is_v018_operation" in fn, f"Founder {name} action does not branch to v0.18 Work control")

    active_run = function_source("eason_one/services/stabilization.py", "active_execution_run")
    require("is_v018_operation" in active_run, "Global runtime can still select historical RUNNING AgentRuns")
    working = function_source("eason_one/services/project_company.py", "_working_people")
    require("is_v018_operation" in working, "Company Floor can still show historical RUNNING AgentRuns")
    employee_activity = function_source("eason_one/services/company_truth.py", "employee_activity")
    require("is_v018_operation" in employee_activity, "Employee presence is not restricted to v0.18 truth")

    require(global_runtime.count('fetch("/api/headquarters/runtime-focus"') == 1,
            "Global runtime focus must have exactly one polling fetch")
    require('fetch("/api/headquarters/runtime-focus"' not in headquarters_js,
            "Project page created a second runtime-focus polling loop")
    for phase in ("INSPECT", "PREPARE", "IMPLEMENT", "VERIFY", "DELIVER"):
        require(phase in stabilization and phase in global_runtime,
                f"Founder phase {phase} is missing from runtime projection")

    primary_templates = "\n".join(text(rel) for rel in (
        "eason_one/templates/hq_finance.html",
        "eason_one/templates/hq_employee.html",
        "eason_one/templates/hq_runs.html",
        "eason_one/templates/hq_research.html",
        "eason_one/templates/costs.html",
    ))
    require("Auditable real spend" not in primary_templates, "UI still claims local estimates are real provider spend")
    require("Attributed AI cost" not in primary_templates, "Employee UI still claims attributed provider billing")
    require("LOCAL MODEL COST" in primary_templates.upper(), "Local model-cost semantics are not visible")

    print("v0.18 Core Cutover structural audit: PASS")
    print("This audit proves source invariants only; it is not live product acceptance.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"v0.18 Core Cutover structural audit: FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
