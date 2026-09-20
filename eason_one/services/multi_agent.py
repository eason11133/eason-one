"""Bounded Multi-Agent work orchestration for Eason One.

V1 deliberately does one thing: take the CEO-approved Task set and determine a
safe dependency graph, then execute independent non-Engineer branches in
parallel.  It does not create scope, invent Employees, or bypass Governance.
Persistent Employees remain accountable; parallel branches are execution
workers underneath the approved company plan.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from typing import Any

from flask import current_app

from ..extensions import db
from ..models import AgentRun, Employee, Operation, Task, Work
from ..schemas import MULTI_AGENT_ORCHESTRATION_SCHEMA
from .execution import execute
from .execution_policy import select_execution_model
from . import operation_kernel as kernel

POLICY_VERSION = "ma-work-v1"
MAX_PARALLELISM = 4
ROLES = {"WORKER", "SYNTHESIS", "VERIFIER", "RESOLVER"}


def enabled(operation: Operation) -> bool:
    memory = dict(operation.memory_json or {})
    return bool(
        memory.get("multi_agent_policy_version") == POLICY_VERSION
        and memory.get("multi_agent_enabled")
        and len(operation.tasks) > 1
    )


def current_plan(operation: Operation) -> dict[str, Any] | None:
    value = dict(operation.memory_json or {}).get("multi_agent_orchestration")
    return dict(value) if isinstance(value, dict) else None


def _task_model(task: Task, operation: Operation | None = None) -> dict[str, Any]:
    """Return planner context without pretending an invalid model is executable.

    Orchestration is allowed to reason about approved Work topology, but a model
    selection failure is execution truth.  Falling back to ``employee.current_model``
    used to make the planner believe a wrong-provider or otherwise ineligible
    model would run even when Runtime would later refuse it.  Keep the Task in
    the topology context, expose the selection problem, and never manufacture an
    executable provider identity.
    """
    employee = task.assigned_employee
    work = db.session.get(Work, task.work_id) if task.work_id else None
    model = None
    model_error = None
    if employee is not None:
        try:
            model = select_execution_model(employee, operation, "TASK_EXECUTION")
        except (ValueError, RuntimeError) as exc:
            model_error = f"{type(exc).__name__}: {exc}"
    return {
        "task_id": task.id,
        "title": task.title,
        "objective": task.objective,
        "employee_id": getattr(employee, "id", None),
        "employee": getattr(employee, "name", None),
        "employee_slug": getattr(employee, "slug", None),
        "provider": getattr(model, "provider_key", None),
        "model": getattr(model, "model_name", None),
        "execution_model_error": model_error,
        "reviewer_employee_id": task.reviewer_employee_id,
        "required_capabilities": list((((getattr(work, "runtime_control_json", None) or {}).get("staffing_requirements") or {}).get("required_capabilities") or [])),
        "acceptance_criteria": task.acceptance_criteria,
    }


def orchestration_context(operation: Operation) -> str:
    rows = [_task_model(task, operation) for task in operation.tasks]
    lines = [
        f"Operation #{operation.id}: {operation.title}",
        f"Objective: {operation.objective}",
        "Approved Work envelopes (scope/capability are immutable; current assignee may be a provisional Team Formation placeholder):",
    ]
    for row in rows:
        lines.extend([
            f"Task #{row['task_id']} — {row['title']}",
            f"  objective: {row['objective']}",
            f"  current_assignee: Employee #{row['employee_id']} {row['employee']} ({row['employee_slug']})",
            f"  intelligence: {row['provider']} / {row['model']}",
            f"  execution_model_error: {row['execution_model_error'] or '-'}",
            f"  reviewer_employee_id: {row['reviewer_employee_id']}",
            f"  required_capabilities: {', '.join(row['required_capabilities']) or '-'}",
            f"  acceptance: {row['acceptance_criteria'] or '-'}",
        ])
    return "\n".join(lines)


def _serial_plan(operation: Operation, reason: str) -> dict[str, Any]:
    previous: int | None = None
    rows = []
    for task in operation.tasks:
        rows.append({
            "task_id": task.id,
            "depends_on_task_ids": [previous] if previous is not None else [],
            "role": "WORKER",
            "reason": "Safe serial fallback preserving the approved Task order.",
        })
        previous = task.id
    return {
        "version": POLICY_VERSION,
        "strategy": "SERIAL",
        "rationale": reason,
        "max_parallelism": 1,
        "tasks": rows,
        "fallback": True,
    }


def serial_fallback(operation: Operation, reason: str) -> dict[str, Any]:
    """Historical compatibility fallback.

    Current governed Company Runtime must prefer :func:`conservative_fallback`
    because arbitrary approved-Task order is not dependency truth.
    """
    return _serial_plan(operation, reason)


_FALLBACK_INDEPENDENCE_MARKERS = (
    "independent", "standalone", "in parallel", "parallel branch",
    "without depending", "without waiting",
)
_FALLBACK_SYNTHESIS_MARKERS = (
    "synthes", "integrat", "reconcile", "combine", "consolidat",
    "accepted predecessor", "upstream evidence", "upstream artifact",
    "handoff", "finalize from", "finalise from",
)
_FALLBACK_REVIEW_MARKERS = (
    "review accepted", "verify accepted", "validate accepted",
    "audit deliver", "critique deliver", "review final", "verify final",
    "independent review of",
)
_FALLBACK_RESEARCH_INPUT_MARKERS = (
    "research finding", "research evidence", "research artifact",
    "research result", "accepted research", "market evidence",
    "source evidence", "external evidence", "current external",
    "cited source", "cited evidence",
)
_FALLBACK_RESEARCH_PRODUCER_MARKERS = (
    "for downstream", "to inform", "for product", "for engineering",
    "for decision", "for strategy", "current external facts",
    "downstream work", "downstream decision",
)
_FALLBACK_PRODUCT_INPUT_MARKERS = (
    "product brief", "approved brief", "approved strategy",
    "product requirement", "requirements", "design spec",
    "approved design", "product specification",
)
_FALLBACK_RESEARCH_CONSUMER_CAPABILITIES = {
    "PRODUCT_STRATEGY", "PRODUCT_DESIGN", "MARKETING", "CONTENT",
    "LEGAL_COMPLIANCE", "FINANCE", "OPERATIONS",
    "CRITICAL_REVIEW", "SOFTWARE_ENGINEERING",
}


def _fallback_task_text(task: Task) -> str:
    return " ".join(
        str(value or "").casefold()
        for value in (task.title, task.objective, task.acceptance_criteria)
    )


def _fallback_task_capability(task: Task) -> str | None:
    work = db.session.get(Work, task.work_id) if task.work_id else None
    values = list(
        (((getattr(work, "runtime_control_json", None) or {}).get("staffing_requirements") or {}).get("required_capabilities") or [])
    )
    if len(values) != 1:
        return None
    value = str(values[0] or "").strip().upper()
    return value or None


def _has_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _fallback_dependencies(task: Task, previous: list[Task]) -> tuple[list[int], str, str]:
    """Infer only dependency intent that is explicit enough to fail safely.

    Approved Task order is never treated as dependency truth.  The deterministic
    fallback uses formal capability tags plus strong language already present in
    the approved Work envelope.  Ambiguous branches remain independent and are
    still protected by normal Work verification/recovery.
    """
    if not previous:
        return [], "WORKER", "First approved branch has no explicit predecessor."

    text = _fallback_task_text(task)
    capability = _fallback_task_capability(task)
    by_capability: dict[str, list[Task]] = {}
    for row in previous:
        cap = _fallback_task_capability(row)
        if cap:
            by_capability.setdefault(cap, []).append(row)

    # Explicit synthesis/final-handoff language is the strongest signal: it says
    # this Work exists to consume earlier approved company outputs.
    if _has_any(text, _FALLBACK_SYNTHESIS_MARKERS):
        return [row.id for row in previous], "SYNTHESIS", "Approved Work explicitly consumes/reconciles upstream outputs."

    # A CRITICAL_REVIEW Work is not automatically downstream: a Critic may own an
    # independent challenge branch.  Only explicit review-of-delivery wording
    # makes it a verifier dependency layer.
    if capability == "CRITICAL_REVIEW" and _has_any(text, _FALLBACK_REVIEW_MARKERS):
        dependencies = [row.id for row in previous if _fallback_task_capability(row) != "CRITICAL_REVIEW"]
        if dependencies:
            return dependencies, "VERIFIER", "Approved review Work explicitly verifies earlier delivery outputs."

    independent = _has_any(text, _FALLBACK_INDEPENDENCE_MARKERS)

    research_rows = by_capability.get("RESEARCH", [])
    if research_rows and not independent:
        explicit_consumer = _has_any(text, _FALLBACK_RESEARCH_INPUT_MARKERS)
        explicit_producer = any(
            _has_any(_fallback_task_text(row), _FALLBACK_RESEARCH_PRODUCER_MARKERS)
            for row in research_rows
        )
        if explicit_consumer or (explicit_producer and capability in _FALLBACK_RESEARCH_CONSUMER_CAPABILITIES):
            return [row.id for row in research_rows], "WORKER", "Approved Work consumes the preceding sourced Research output."

    if capability == "SOFTWARE_ENGINEERING" and not independent and _has_any(text, _FALLBACK_PRODUCT_INPUT_MARKERS):
        product_rows = [
            row for row in previous
            if _fallback_task_capability(row) in {"PRODUCT_STRATEGY", "PRODUCT_DESIGN"}
        ]
        if product_rows:
            return [row.id for row in product_rows], "WORKER", "Engineering Work explicitly consumes an approved product/design brief."

    return [], "WORKER", "No explicit approved dependency signal; preserve this branch as independent."


def _fallback_parallelism(rows: list[dict[str, Any]]) -> int:
    """Return the useful maximum frontier width of the deterministic DAG."""
    deps = {int(row["task_id"]): set(int(value) for value in row["depends_on_task_ids"]) for row in rows}
    remaining = set(deps)
    completed: set[int] = set()
    widest = 1
    while remaining:
        ready = {task_id for task_id in remaining if deps[task_id] <= completed}
        if not ready:  # Defensive; _validate_dag will reject a cycle later.
            return 1
        widest = max(widest, len(ready))
        completed |= ready
        remaining -= ready
    return min(MAX_PARALLELISM, widest)


def conservative_fallback(operation: Operation, reason: str) -> dict[str, Any]:
    """Deterministic degraded topology that never invents a total serial chain.

    This is the current Company Runtime fallback when the optional orchestration
    model is unavailable/invalid.  It preserves explicit accepted dataflow while
    keeping ambiguous approved branches independent.
    """
    tasks = sorted(operation.tasks, key=lambda row: row.id)
    rows: list[dict[str, Any]] = []
    previous: list[Task] = []
    for task in tasks:
        dependencies, role, dependency_reason = _fallback_dependencies(task, previous)
        rows.append({
            "task_id": task.id,
            "depends_on_task_ids": dependencies,
            "role": role,
            "reason": dependency_reason,
        })
        previous.append(task)
    max_parallelism = _fallback_parallelism(rows)
    strategy = "PARALLEL_DAG" if len(tasks) > 1 else "SERIAL"
    if strategy == "SERIAL":
        max_parallelism = 1
    return {
        "version": POLICY_VERSION,
        "strategy": strategy,
        "rationale": (
            "Deterministic conservative topology after optional planner failure. "
            "Approved Task order and provisional assignee are not dependency truth. "
            f"Planner failure: {reason}"
        ),
        "max_parallelism": max_parallelism,
        "tasks": rows,
        "fallback": True,
    }


def _validate_dag(task_ids: set[int], rows: list[dict[str, Any]]) -> None:
    graph = {int(row["task_id"]): [int(value) for value in row["depends_on_task_ids"]] for row in rows}
    if set(graph) != task_ids:
        raise ValueError("Orchestrator must return every approved Task exactly once")
    for task_id, deps in graph.items():
        if len(deps) != len(set(deps)):
            raise ValueError(f"Task #{task_id} repeats a dependency")
        if task_id in deps:
            raise ValueError(f"Task #{task_id} cannot depend on itself")
        unknown = set(deps) - task_ids
        if unknown:
            raise ValueError(f"Task #{task_id} references unknown dependencies {sorted(unknown)}")

    visiting: set[int] = set()
    visited: set[int] = set()

    def visit(node: int) -> None:
        if node in visited:
            return
        if node in visiting:
            raise ValueError("Orchestration dependency graph contains a cycle")
        visiting.add(node)
        for dep in graph[node]:
            visit(dep)
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        visit(node)


def _enforce_research_department_contract(operation: Operation, rows: list[dict[str, Any]], strategy: str, max_parallelism: int) -> None:
    """Protect multi-AI organizational semantics from planner drift.

    The orchestration model may choose ordinary topology, but it may not turn
    independent provider-specialized Researchers into a serial chain or let the
    Research Director synthesize before every selected branch is available.
    """
    department = __import__(
        "eason_one.services.research_department",
        fromlist=["is_specialist", "is_synthesis_text"],
    )
    tasks = list(operation.tasks)
    task_by_id = {task.id: task for task in tasks}
    row_by_id = {int(row["task_id"]): row for row in rows}
    branches = [
        task for task in tasks
        if department.is_specialist(task.assigned_employee)
        and _fallback_task_capability(task) == "RESEARCH"
    ]
    if len(branches) < 2:
        return
    synths = [
        task for task in tasks
        if getattr(task.assigned_employee, "slug", None) == "research-director"
        and department.is_synthesis_text(task.title, task.objective, task.acceptance_criteria)
    ]
    if not synths:
        raise ValueError("Multi-AI Research Department work is missing its accountable Research Director synthesis")
    if len(synths) != 1:
        raise ValueError("Multi-AI Research Department topology has ambiguous multiple Director synthesis Tasks")

    branch_ids = {task.id for task in branches}
    synth = synths[0]
    for branch in branches:
        sibling_deps = set(row_by_id[branch.id]["depends_on_task_ids"]) & branch_ids
        if sibling_deps:
            raise ValueError(
                f"Researcher Task #{branch.id} may not depend on sibling Researcher conclusions {sorted(sibling_deps)}"
            )
    synth_deps = set(row_by_id[synth.id]["depends_on_task_ids"])
    missing = branch_ids - synth_deps
    if missing:
        raise ValueError(
            f"Research Director synthesis must wait for every selected Researcher; missing Tasks {sorted(missing)}"
        )
    if row_by_id[synth.id]["role"] != "SYNTHESIS":
        raise ValueError("Research Director aggregate Work must have SYNTHESIS orchestration role")
    if strategy != "PARALLEL_DAG" or max_parallelism < 2:
        raise ValueError("Independent multi-AI Research Department Work requires a parallel DAG with at least two runnable slots")

    graph = {int(row["task_id"]): set(int(value) for value in row["depends_on_task_ids"]) for row in rows}

    def has_ancestor(node: int, ancestor: int, seen: set[int] | None = None) -> bool:
        if node == ancestor:
            return True
        seen = set() if seen is None else seen
        if node in seen:
            return False
        seen.add(node)
        return any(has_ancestor(dep, ancestor, seen) for dep in graph.get(node, set()))

    # The normalized Director objective explicitly declares itself the canonical
    # research handoff for downstream Work. If a later approved capability is a
    # normal research consumer (and does not explicitly say independent), it must
    # have a dependency path to the Director synthesis. This prevents a valid-DAG
    # planner response from quietly bypassing the whole multi-AI research stage.
    synth_is_producer = _has_any(_fallback_task_text(synth), _FALLBACK_RESEARCH_PRODUCER_MARKERS)
    if synth_is_producer:
        for task in tasks:
            if task.id <= synth.id or task.id in branch_ids:
                continue
            text = _fallback_task_text(task)
            capability = _fallback_task_capability(task)
            consumes = (
                department.is_synthesis_text(task.title, task.objective, task.acceptance_criteria)
                or (
                    capability in _FALLBACK_RESEARCH_CONSUMER_CAPABILITIES
                    and not _has_any(text, _FALLBACK_INDEPENDENCE_MARKERS)
                )
            )
            if consumes and not has_ancestor(task.id, synth.id):
                raise ValueError(
                    f"Downstream Task #{task.id} bypasses the canonical Research Director synthesis"
                )


def validate_payload(operation: Operation, payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Orchestrator did not return an object")
    expected = {"strategy", "rationale", "max_parallelism", "tasks"}
    if set(payload) != expected:
        raise ValueError("Invalid orchestration fields")
    strategy = str(payload.get("strategy") or "").upper()
    if strategy not in {"SERIAL", "PARALLEL_DAG"}:
        raise ValueError("Invalid orchestration strategy")
    rationale = str(payload.get("rationale") or "").strip()
    if not rationale:
        raise ValueError("Orchestration rationale is required")
    max_parallelism = int(payload.get("max_parallelism") or 1)
    if not 1 <= max_parallelism <= MAX_PARALLELISM:
        raise ValueError("max_parallelism is outside the bounded 1-4 range")
    rows = payload.get("tasks")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Orchestration Tasks are required")
    normalized = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"task_id", "depends_on_task_ids", "role", "reason"}:
            raise ValueError("Invalid orchestration Task fields")
        role = str(row.get("role") or "").upper()
        if role not in ROLES:
            raise ValueError("Invalid orchestration Task role")
        normalized.append({
            "task_id": int(row["task_id"]),
            "depends_on_task_ids": [int(value) for value in (row.get("depends_on_task_ids") or [])],
            "role": role,
            "reason": str(row.get("reason") or "").strip() or "Approved work branch.",
        })
    task_ids = {task.id for task in operation.tasks}
    _validate_dag(task_ids, normalized)
    if strategy == "SERIAL":
        max_parallelism = 1
    _enforce_research_department_contract(operation, normalized, strategy, max_parallelism)
    return {
        "version": POLICY_VERSION,
        "strategy": strategy,
        "rationale": rationale,
        "max_parallelism": max_parallelism,
        "tasks": normalized,
        "fallback": False,
    }


def _recover_paid_orchestration_postprocess(
    operation: Operation, *, context: str, system_prompt: str, postprocess
) -> AgentRun | None:
    """Finish local orchestration materialization without rebuying the model call.

    ``execute()`` durably settles the provider response before it invokes the
    optional local postprocess callback. A process crash in that narrow window
    therefore leaves a paid ``SUCCEEDED`` Run with raw output but no parsed DAG.
    A local validation/DB error may similarly leave ``POSTPROCESS_FAILED`` even
    though the exact provider response is already durable and settled.

    Re-entering topology planning must first replay only that deterministic local
    postprocess. It may never buy another orchestration response merely because
    dependency projection had not committed yet. Exact prompt/context hashes keep
    reuse scoped to the same immutable approved Task topology.
    """
    context_hash = hashlib.sha256(context.encode("utf-8")).hexdigest()
    prompt_hash = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()
    effects = __import__(
        "eason_one.models", fromlist=["ExternalEffectAttempt"]
    ).ExternalEffectAttempt
    rows = (
        AgentRun.query.filter_by(
            operation_id=operation.id, purpose="ORCHESTRATION_PLAN",
            context_hash=context_hash, prompt_hash=prompt_hash,
        )
        .order_by(AgentRun.id.desc()).all()
    )
    for run in rows:
        if run.status == "RUNNING" or str(run.outcome or "") == "FAILED_AMBIGUOUS":
            # The caller/runtime owns unresolved-effect handling. Returning the
            # exact Run prevents any direct call-site from purchasing around it.
            return run
        recoverable = bool(
            run.raw_output
            and (
                run.status == "SUCCEEDED"
                or run.failure_reason == "POSTPROCESS_FAILED"
            )
        )
        if not recoverable:
            continue
        effect = (
            effects.query.filter_by(execution_id=run.id)
            .order_by(effects.id.desc()).first()
        )
        if effect is None or str(effect.state or "").upper() not in {"PERSISTED", "SETTLED"}:
            continue
        if run.parsed_output_json:
            return run
        try:
            postprocess(run)
        except Exception as exc:
            # Preserve paid provider truth and the local fault. Another browser
            # refresh may retry this local operation, but not the provider call.
            run.status = "FAILED"
            run.outcome = "FAILED_KNOWN"
            run.failure_reason = "POSTPROCESS_FAILED"
            run.failure_stage = "POSTPROCESS"
            run.error_text = f"Orchestration local materialization failed: {exc}"
            db.session.commit()
            return run
        run.status = "SUCCEEDED"
        run.outcome = "SUCCEEDED"
        run.failure_reason = None
        run.failure_stage = None
        run.error_text = None
        composition = dict(run.context_composition_json or {})
        recovery = dict(composition.get("orchestration_local_recovery") or {})
        recovery.update({
            "recovered": True,
            "provider_replayed": False,
            "source_run_id": run.id,
        })
        composition["orchestration_local_recovery"] = recovery
        run.context_composition_json = composition
        db.session.commit()
        return run
    return None


def call_orchestrator(operation: Operation) -> AgentRun:
    ceo = operation.proposed_by or Employee.query.filter_by(slug="ceo").first()
    if not ceo:
        raise ValueError("Persistent CEO is required for orchestration")
    selected_model = select_execution_model(ceo, operation, "ORCHESTRATION_PLAN")
    # Topology is bounded to the already-approved Task set, but 900 tokens can
    # still truncate a 3-4 branch DAG with rationale/dependency rows. Size the
    # envelope to the approved graph and keep the deterministic conservative
    # fallback as the final safety net if the model still fails.
    task_count = max(1, len(operation.tasks))
    output_cap = min(
        max(1200, 600 + 320 * task_count),
        int(selected_model.max_output_tokens or 1200),
    )
    system_prompt = (
        "MULTI_AGENT_ORCHESTRATION_V1\nYou are the Eason One execution orchestrator. The Founder and CEO have already approved "
        "the scope, Tasks, required capabilities, reviewers, budget and authority. You may NOT create, remove, "
        "rewrite, reassign or widen any Task. Current assignees may be provisional placeholders while Company Team Formation "
        "matches or hires the accountable specialist before execution, so never serialize independent Work merely because multiple "
        "Tasks temporarily show the same CEO/manager assignee. Decide only execution topology from Work purpose, required capability "
        "and true Artifact dependencies. Mark genuinely "
        "independent Tasks with no dependency so Runtime can run them concurrently. Add a dependency "
        "only when a Task actually needs another Task's persisted result. Required capability tags are authoritative staffing semantics. "
        "When an approved RESEARCH Task exists specifically to establish current external facts for later PRODUCT_STRATEGY, PRODUCT_DESIGN, "
        "MARKETING, CONTENT, LEGAL_COMPLIANCE, FINANCE, OPERATIONS, CRITICAL_REVIEW, or SOFTWARE_ENGINEERING Work, that downstream Work must depend on the relevant RESEARCH Task so it consumes the accepted sourced Artifact rather than inventing current facts independently. "
        "Use PARALLEL_DAG when safe; otherwise SERIAL. max_parallelism must be 1-4 and proportional to useful independent work. "
        "Repository-mutating Engineer/Codex work will be serialized by Runtime regardless of your plan. "
        "Roles describe function only: WORKER, SYNTHESIS, VERIFIER, RESOLVER. Do not manufacture debate "
        "or redundant work just to use more agents. Return only the required structured result."
    )
    def _postprocess(run):
        payload = json.loads(run.raw_output or "{}")
        normalized = validate_payload(operation, payload)
        run.parsed_output_json = {
            key: normalized[key] for key in ("strategy", "rationale", "max_parallelism", "tasks")
        }
        run.structured_validation_status = "PASSED"
        run.structured_validation_errors_json = []
        db.session.commit()

    context = orchestration_context(operation)
    recovered = _recover_paid_orchestration_postprocess(
        operation, context=context, system_prompt=system_prompt, postprocess=_postprocess
    )
    if recovered is not None:
        return recovered

    management_work = __import__(
        "eason_one.services.work_runtime", fromlist=["ensure_management_work"]
    ).ensure_management_work(operation)
    return execute(
        ceo,
        "ORCHESTRATION_PLAN",
        "Determine the smallest safe execution graph for the already-approved Tasks.",
        project=operation.project,
        context_override=context,
        system_prompt_override=system_prompt,
        response_schema=MULTI_AGENT_ORCHESTRATION_SCHEMA,
        max_output_tokens_override=output_cap,
        operation=operation,
        work=management_work,
        model_override=selected_model,
        prompt_version="multi_agent_orchestration-v1",
        context_composition={"multi_agent": {"policy_version": POLICY_VERSION, "task_count": len(operation.tasks)}},
        postprocess=_postprocess,
    )


def persist_plan(operation: Operation, plan: dict[str, Any], *, planner_run_id: int | None = None) -> dict[str, Any]:
    normalized = validate_payload(operation, {key: plan[key] for key in ("strategy", "rationale", "max_parallelism", "tasks")}) if "version" not in plan else dict(plan)
    if "version" in normalized:
        # Re-validate persisted/fallback plans while preserving internal metadata.
        core = {key: normalized[key] for key in ("strategy", "rationale", "max_parallelism", "tasks")}
        checked = validate_payload(operation, core)
        checked["fallback"] = bool(normalized.get("fallback"))
        normalized = checked
    normalized["planner_run_id"] = planner_run_id
    memory = dict(operation.memory_json or {})
    memory["multi_agent_orchestration"] = normalized
    memory["multi_agent_current_wave"] = None
    operation.memory_json = memory
    __import__(
        "eason_one.services.work_runtime", fromlist=["set_dependencies_from_task_plan"]
    ).set_dependencies_from_task_plan(operation, normalized["tasks"])
    kernel.append_event(
        operation,
        "ORCHESTRATION_PLANNED",
        stage="ORCHESTRATION",
        actor_type="CEO" if planner_run_id else "RUNTIME",
        actor_ref=str(planner_run_id) if planner_run_id else None,
        idempotency_key=f"orchestration-plan:{POLICY_VERSION}",
        payload={
            "strategy": normalized["strategy"],
            "max_parallelism": normalized["max_parallelism"],
            "task_count": len(normalized["tasks"]),
            "fallback": bool(normalized.get("fallback")),
        },
    )
    db.session.commit()
    return normalized


def dependencies_for_task(operation: Operation, task_id: int) -> list[int]:
    plan = current_plan(operation) or {}
    row = next((item for item in (plan.get("tasks") or []) if int(item.get("task_id")) == int(task_id)), None)
    return [int(value) for value in ((row or {}).get("depends_on_task_ids") or [])]


def task_role(operation: Operation, task_id: int) -> str:
    plan = current_plan(operation) or {}
    row = next((item for item in (plan.get("tasks") or []) if int(item.get("task_id")) == int(task_id)), None)
    return str((row or {}).get("role") or "WORKER")


def ready_tasks(operation: Operation) -> list[Task]:
    plan = current_plan(operation)
    if not plan:
        return []
    work_runtime = __import__(
        "eason_one.services.work_runtime",
        fromlist=["dependencies_satisfied", "sync_task_projection", "work_for_task"],
    )
    by_id = {task.id: task for task in operation.tasks}
    ready = []
    for task in operation.tasks:
        work = work_runtime.work_for_task(task)
        if work:
            # Company Core vNext: Work + WorkDependency are authoritative.  Task
            # is only an execution adapter and stale legacy status may not veto a
            # runnable Work or re-introduce a second dependency truth.
            if work.state not in {"READY", "EXECUTING"}:
                continue
            if not work_runtime.dependencies_satisfied(work):
                continue
            work_runtime.sync_task_projection(work, task)
            ready.append(task)
            continue

        # Pre-vNext compatibility only.
        if task.status not in {"ASSIGNED", "WORKING"}:
            continue
        deps = dependencies_for_task(operation, task.id)
        if all(by_id.get(dep) is not None and by_id[dep].status == "DONE" for dep in deps):
            ready.append(task)
    return ready


def blocked_by_dependencies(operation: Operation) -> list[Task]:
    work_runtime = __import__(
        "eason_one.services.work_runtime",
        fromlist=["dependency_blocked_tasks", "work_for_task"],
    )
    if any(work_runtime.work_for_task(task) is not None for task in operation.tasks):
        return work_runtime.dependency_blocked_tasks(operation)
    ready_ids = {task.id for task in ready_tasks(operation)}
    return [
        task for task in operation.tasks
        if task.status in {"ASSIGNED", "WORKING"} and task.id not in ready_ids
    ]


def select_wave(operation: Operation) -> list[Task]:
    plan = current_plan(operation) or {}
    candidates = ready_tasks(operation)
    if not candidates:
        return []
    cap = max(1, min(int(plan.get("max_parallelism") or 1), MAX_PARALLELISM))
    if plan.get("strategy") == "SERIAL":
        cap = 1
    # Repository-mutating Engineer/Codex work remains single-writer in V1.
    non_engineers = [task for task in candidates if not (task.assigned_employee and task.assigned_employee.slug == "engineer")]
    if non_engineers:
        return non_engineers[:cap]
    return candidates[:1]


def _worker(app, operation_id: int, task_id: int, request_key: str) -> dict[str, Any]:
    with app.app_context():
        try:
            operation = db.session.get(Operation, operation_id)
            task = db.session.get(Task, task_id)
            if not operation or not task:
                return {"kind": "TASK", "status": "MISSING", "task_id": task_id}
            operations = __import__("eason_one.services.operations", fromlist=["_run_task_step"])
            return operations._run_task_step(operation, request_key, task)
        except Exception as exc:
            db.session.rollback()
            operation = db.session.get(Operation, operation_id)
            task = db.session.get(Task, task_id)
            if task and task.status not in {"DONE", "CANCELLED", "BLOCKED"}:
                task.status = "BLOCKED"
            if task and task.work_id:
                work=__import__("eason_one.services.work_runtime",fromlist=["work_for_task","open_wait"]).work_for_task(task)
                if work and work.state not in {"ACCEPTED","ABANDONED","CANCELLED"}:
                    __import__("eason_one.services.work_runtime",fromlist=["open_wait"]).open_wait(
                        work,"INTERNAL_RECOVERY",f"Worker exception requires local management recovery: {exc}"
                    )
            # Do not freeze the whole Operation from inside one parallel worker.
            # The Work remains durable and Project management can replan it.
            db.session.commit()
            return {"kind": "TASK", "status": "FAILED", "task_id": task_id, "error": str(exc)}
        finally:
            db.session.remove()


def run_parallel_wave(operation: Operation, tasks: list[Task], request_key: str) -> dict[str, Any]:
    if len(tasks) < 2:
        raise ValueError("Parallel wave requires at least two ready Tasks")
    app = current_app._get_current_object()
    task_ids = [task.id for task in tasks]
    memory = dict(operation.memory_json or {})
    memory["multi_agent_current_wave"] = {"task_ids": task_ids, "status": "RUNNING"}
    operation.memory_json = memory
    kernel.append_event(
        operation,
        "PARALLEL_WAVE_STARTED",
        stage="EXECUTION",
        actor_type="RUNTIME",
        idempotency_key=f"{request_key}:parallel-start",
        payload={"task_ids": task_ids, "parallelism": len(task_ids)},
    )
    db.session.commit()

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=len(task_ids), thread_name_prefix=f"eason-ma-{operation.id}") as pool:
        futures = {
            pool.submit(_worker, app, operation.id, task_id, f"{request_key}:task:{task_id}"): task_id
            for task_id in task_ids
        }
        for future in as_completed(futures):
            task_id = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:  # Worker is defensive; this is last-resort truth.
                results.append({"kind": "TASK", "status": "FAILED", "task_id": task_id, "error": str(exc)})

    db.session.expire_all()
    operation = db.session.get(Operation, operation.id)
    failed = [row for row in results if row.get("status") not in {"SUCCEEDED", "DONE", "COMPLETED"}]
    succeeded = [row for row in results if row not in failed]
    wave_status = "COMPLETED" if not failed else ("PARTIAL" if succeeded else "BLOCKED")
    memory = dict(operation.memory_json or {})
    memory["multi_agent_current_wave"] = {"task_ids": task_ids, "status": wave_status}
    # Legacy deferred Founder-attention rows are intentionally consumed here;
    # branch execution failure is no longer an Operation-wide Founder gate.
    memory.pop("multi_agent_deferred_attention", None)
    operation.memory_json = memory
    kernel.append_event(
        operation,
        "PARALLEL_WAVE_COMPLETED",
        stage="EXECUTION",
        actor_type="RUNTIME",
        idempotency_key=f"{request_key}:parallel-complete",
        payload={
            "task_ids": task_ids,
            "wave_status": wave_status,
            "results": [{"task_id": row.get("task_id"), "status": row.get("status")} for row in results],
        },
    )
    db.session.commit()
    results.sort(key=lambda row: int(row.get("task_id") or 0))
    return {
        "kind": "PARALLEL_WAVE",
        "status": wave_status,
        "task_ids": task_ids,
        "task_results": results,
    }


def projection(operation: Operation) -> dict[str, Any] | None:
    plan = current_plan(operation)
    if not plan:
        return None
    by_id = {task.id: task for task in operation.tasks}
    work_by_task_id = {
        task.id: db.session.get(Work, task.work_id)
        for task in operation.tasks
        if getattr(task, "work_id", None) is not None
    }
    current_wave = dict(operation.memory_json or {}).get("multi_agent_current_wave") or {}
    rows = []
    for item in plan.get("tasks") or []:
        task = by_id.get(int(item["task_id"]))
        if not task:
            continue
        work = work_by_task_id.get(task.id)
        assignment = (
            __import__("eason_one.services.work_runtime", fromlist=["active_assignment"]).active_assignment(work)
            if work is not None else None
        )
        employee = assignment.employee if assignment else task.assigned_employee
        try:
            model = select_execution_model(employee, operation, "TASK_EXECUTION") if employee else None
        except Exception:
            model = getattr(employee, "current_model", None)
        deps = [int(value) for value in item.get("depends_on_task_ids") or []]
        dependency_names = [by_id[value].title for value in deps if value in by_id]
        if work is not None:
            work_runtime = __import__(
                "eason_one.services.work_runtime", fromlist=["dependencies_satisfied", "has_open_gate"]
            )
            ready = (
                work.state in {"READY", "EXECUTING", "VERIFYING"}
                and not work_runtime.has_open_gate(work)
                and (work.state == "VERIFYING" or work_runtime.dependencies_satisfied(work))
            )
        else:
            # Historical operations remain readable through their Task projection.
            ready = task.status in {"ASSIGNED", "WORKING"} and all(
                by_id.get(dep) and by_id[dep].status == "DONE" for dep in deps
            )
        experience_summary = (
            __import__(
                "eason_one.services.employee_memory", fromlist=["roster_experience_summary"]
            ).roster_experience_summary(employee)
            if employee is not None else "accepted experience: unavailable"
        )
        rows.append({
            "task": task,
            "work": work,
            "role": item.get("role") or "WORKER",
            "reason": item.get("reason") or "",
            "dependencies": deps,
            "dependency_names": dependency_names,
            "ready": ready,
            "employee": employee,
            "provider": getattr(model, "provider_key", None),
            "model": getattr(model, "model_name", None),
            "experience_summary": experience_summary,
            "in_current_wave": bool(
                work is not None and work.state == "EXECUTING"
            ) if getattr(operation, "runtime_protocol_version", None) else (
                task.id in (current_wave.get("task_ids") or []) and current_wave.get("status") == "RUNNING"
            ),
        })
    return {
        "operation": operation,
        "strategy": plan.get("strategy"),
        "rationale": plan.get("rationale"),
        "max_parallelism": int(plan.get("max_parallelism") or 1),
        "fallback": bool(plan.get("fallback")),
        "planner_run_id": plan.get("planner_run_id"),
        "tasks": rows,
        "current_wave": current_wave,
    }
