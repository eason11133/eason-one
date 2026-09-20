from copy import deepcopy
from decimal import Decimal, InvalidOperation

from ..extensions import db
from ..models import KnowledgeItem, Operation
from . import operations


ACTIONS={"APPROVE","REJECT","MODIFY"}
PENDING={"PLANNED","WAITING_FOR_FOUNDER"}


def _is_governed_vnext(operation) -> bool:
    project = getattr(operation, "project", None)
    if not project:
        return False
    return __import__(
        "eason_one.services.project_contract", fromlist=["is_vnext_governed"]
    ).is_vnext_governed(project)


def _record(operation, action, reason, changes=None, *, budget_decision=False):
    memory=dict(operation.memory_json or {})
    decisions=list(memory.get("founder_decisions") or [])
    record={"authority":"FOUNDER","action":action,"reason":reason,
      "changes":changes or {}}
    decisions.append(record)
    memory["founder_decisions"]=decisions
    events=list(memory.get("founder_attention_events") or [])
    if events and events[-1].get("status")=="PENDING":
        events[-1]=dict(events[-1],status="RESOLVED",resolution=action)
        memory["founder_attention_events"]=events
    if changes:
        constraints=list(memory.get("founder_constraints") or [])
        constraints.append(changes)
        memory["founder_constraints"]=constraints
    operation.memory_json=memory
    if __import__(
        "eason_one.services.core_v018", fromlist=["is_v018_operation"]
    ).is_v018_operation(operation):
        __import__("eason_one.services.company_events",fromlist=["emit"]).emit(
            "FOUNDER_DECISION_RECORDED", actor_type="FOUNDER",
            project_id=operation.project_id, correlation_id=f"project:{operation.project_id}",
            payload={"operation_id": operation.id, "action":action,"reason":reason,"changes":changes or {}},
        )
    else:
        __import__("eason_one.services.operation_kernel",fromlist=["append_event"]).append_event(
          operation,"FOUNDER_DECISION_RECORDED",
          from_status=__import__("eason_one.services.operation_kernel",fromlist=["authoritative_status"]).authoritative_status(operation),
          to_status=__import__("eason_one.services.operation_kernel",fromlist=["authoritative_status"]).authoritative_status(operation),
          stage=operation.current_stage,actor_type="FOUNDER",
          payload={"action":action,"reason":reason,"changes":changes or {}},
        )
    # Budget authority is Operation governance, not work evidence. Injecting it
    # into Company Brain changes the very provider estimate it just authorized.
    if not budget_decision:
        db.session.add(KnowledgeItem(
          project_id=operation.project_id,kind="DECISION",
          title=f"Founder {action.lower()}: {operation.title}",
          content=reason,rationale=reason,founder_approved=True))
    db.session.commit()
    return record


def _authorize_additional_budget(operation, amount, *, scope=None):
    amount=operations.budget_authorization_amount(amount)
    is_v018 = __import__(
        "eason_one.services.core_v018", fromlist=["is_v018_operation"]
    ).is_v018_operation(operation)
    scope = str(scope or "PROJECT" if is_v018 else "MISSION").upper()
    if is_v018 and scope == "PROJECT":
        if not operation.project:
            raise ValueError("v0.20 Project budget authority requires a Project")
        __import__(
            "eason_one.services.project_contract", fromlist=["authorize_budget_extension"]
        ).authorize_budget_extension(
            operation.project, additional_twd=amount,
            reason="Founder authorized additional Project budget.",
            origin_employee_id=operation.project.owner_employee_id,
            source_ref=f"project:{operation.project.id}:founder_budget_extension:operation:{operation.id}",
        )
        db.session.commit()
        return
    if operation.project:
        remaining = operations.project_remaining_authority(operation.project)
        operation_remaining = max(Decimal("0"), operations.remaining_budget(operation))
        if remaining is not None and operation_remaining + amount > remaining:
            raise ValueError(
                "Additional Operation authority exceeds the remaining Founder-approved Project budget. "
                "Modify Project authority explicitly instead of silently expanding it."
            )
    operation.approved_budget_twd = Decimal(operation.approved_budget_twd) + amount
    operation.hard_cost_cap_twd = operation.approved_budget_twd
    db.session.commit()


def _canonical_vnext_gate(operation):
    project = operation.project if operation else None
    if not project:
        return None
    contracts = __import__(
        "eason_one.services.project_contract", fromlist=["is_vnext_governed"]
    )
    if not contracts.is_vnext_governed(project) or operation.approved_at is None:
        return None
    governance = __import__(
        "eason_one.services.governance", fromlist=["current_gate"]
    )
    current = governance.current_gate(project)
    if current is None:
        return None
    report = dict(operation.founder_report_json or {})
    escalation_id = report.get("governance_escalation_id")
    if escalation_id:
        # Mission compatibility projections may be stale or may point at a later
        # queued authority question. They can resolve only the FIFO current gate;
        # otherwise Founder would be able to answer Project authority out of order.
        if int(escalation_id) != current.id:
            return None
        if current.operation_id not in {None, operation.id}:
            return None
        return current
    if current.operation_id in {None, operation.id}:
        return current
    return None


def _resolve_canonical_vnext(operation, gate, action, *, reason, budget_twd=None):
    governance = __import__(
        "eason_one.services.governance", fromlist=["normalize_type", "resolve_gate"]
    )
    kind = governance.normalize_type(gate.escalation_type)
    changes = None
    if action == "MODIFY":
        if kind == "BUDGET_AUTHORIZATION":
            if budget_twd in {None, ""}:
                raise ValueError("Modified Project budget authorization requires an exact additional amount")
            try:
                amount = Decimal(str(budget_twd))
            except (InvalidOperation, TypeError) as exc:
                raise ValueError("Modified Project budget authorization must be numeric") from exc
            if amount <= 0:
                raise ValueError("Modified Project budget authorization must be positive")
            changes = {"additional_budget_twd": str(amount), "scope": "PROJECT"}
        else:
            raise ValueError(
                f"{kind} must be modified from the Project Governance surface with exact Contract terms."
            )
    decision = governance.resolve_gate(
        gate, action, reason=reason, changes=changes
    )
    operation.founder_report_json = None
    operation.waiting_reason = None
    db.session.commit()
    __import__(
        "eason_one.services.company_runtime", fromlist=["wake_company_runtime"]
    ).wake_company_runtime()
    return decision


def decide(operation, action, *, reason=None, objective=None,
           budget_twd=None, completion_criteria=None):
    core = __import__(
        "eason_one.services.core_v018",
        fromlist=["is_dormant_legacy_project_operation"],
    )
    if core.is_dormant_legacy_project_operation(operation):
        raise ValueError(
            "This pre-v0.18 Project decision is historical and is no longer actionable."
        )
    action=(action or "").upper()
    if action not in ACTIONS:
        raise ValueError("Unknown Founder decision")
    canonical_gate = _canonical_vnext_gate(operation)
    governed_vnext = _is_governed_vnext(operation)
    # Once a vNext Mission is approved, all new Founder authority must be an
    # exact canonical Governance gate. Legacy WAITING_FOR_FOUNDER/report fields
    # and Work waits cannot manufacture a second decision path.
    if governed_vnext and operation.approved_at is not None and not canonical_gate:
        raise ValueError("Operation has no current canonical Founder Governance gate")
    if operation.status not in PENDING and not canonical_gate:
        raise ValueError("Operation has no pending Founder decision")
    reason=(reason or "").strip()
    if canonical_gate:
        return _resolve_canonical_vnext(
            operation, canonical_gate, action,
            reason=reason or f"Founder {action.lower()}d the exact Project governance question.",
            budget_twd=budget_twd,
        )
    if action=="REJECT":
        reason=reason or "Founder rejected the pending authority."
        operations.stop(operation)
        return _record(operation,"REJECT",reason)
    if action=="APPROVE":
        reason=reason or "Founder approved the pending authority."
        if operation.approved_at is None:
            operations.approve(operation)
            return _record(operation,"APPROVE",reason)
        else:
            report=operation.founder_report_json or {}
            budget_decision=(
              report.get("decision_kind")=="BUDGET_AUTHORIZATION")
            additional=report.get(
              "additional_budget_twd")
            if additional:
                _authorize_additional_budget(operation,additional,scope=report.get("scope"))
            if (operation.memory_json or {}).get("runtime_semantics") == "WORK_CORE_V018":
                __import__(
                    "eason_one.services.company_runtime", fromlist=["resume_after_founder"]
                ).resume_after_founder(operation, resolution="APPROVED")
            else:
                operations.resume(operation)
            record=_record(
              operation,"APPROVE",reason,budget_decision=budget_decision)
            operation.founder_report_json=None
            db.session.commit()
            return record

    changes={}
    if objective and objective.strip():
        changes["objective"]=objective.strip()
    if budget_twd not in {None,""}:
        try: budget=Decimal(str(budget_twd))
        except (InvalidOperation,TypeError):
            raise ValueError("Modified budget must be numeric")
        if budget<=0:
            raise ValueError("Modified budget must be positive")
        if (
          operation.status=="WAITING_FOR_FOUNDER"
          and (operation.founder_report_json or {}).get("decision_kind")
          =="BUDGET_AUTHORIZATION"
        ):
            budget=operations.budget_authorization_amount(budget)
        changes["budget_twd"]=str(budget)
    criteria=[item.strip() for item in (completion_criteria or "").splitlines()
      if item.strip()]
    if criteria:
        changes["completion_criteria"]=criteria
    if not changes and not reason:
        raise ValueError("Founder modifications are required")
    reason=reason or "Founder approved the operation with changes."

    plan=deepcopy(operation.plan_json)
    data=plan["operation"]
    if "objective" in changes:
        data["objective"]=changes["objective"]
    if "completion_criteria" in changes:
        data["completion_criteria"]=changes["completion_criteria"]
    if "budget_twd" in changes:
        data["budget_twd"]=(
          changes["budget_twd"] if operation.approved_at is None else
          str(operation.approved_budget_twd))
    operations.validate_plan(plan)
    operation.plan_json=plan
    operation.objective=data["objective"]
    if operation.approved_at is None:
        operation.approved_budget_twd=Decimal(str(data["budget_twd"]))
        operation.hard_cost_cap_twd=operation.approved_budget_twd
    if operation.project:
        # v0.20 Mission modification is not Project Contract modification.
        # The immutable Project objective/success criteria remain unchanged;
        # only pre-v0.18 compatibility Operations may retain the historical
        # projection behavior.
        is_v020 = __import__(
            "eason_one.services.core_v018", fromlist=["is_v018_operation"]
        ).is_v018_operation(operation)
        if not is_v020:
            operation.project.objective=data["objective"]
    db.session.commit()

    if operation.approved_at is None:
        operations.approve(operation)
    else:
        memory=dict(operation.memory_json or {})
        memory["pending_founder_modification"]=changes
        operation.memory_json=memory
        db.session.commit()
        if "budget_twd" in changes:
            _authorize_additional_budget(operation,changes["budget_twd"],scope=(operation.founder_report_json or {}).get("scope"))
        if (operation.memory_json or {}).get("runtime_semantics") == "WORK_CORE_V018":
            __import__(
                "eason_one.services.company_runtime", fromlist=["resume_after_founder"]
            ).resume_after_founder(operation, resolution="MODIFIED")
        else:
            operations.resume(operation)
    budget_decision=(
      operation.status=="RUNNING"
      and (operation.founder_report_json or {}).get("decision_kind")
      =="BUDGET_AUTHORIZATION")
    record=_record(
      operation,"MODIFY",reason,changes,budget_decision=budget_decision)
    if budget_decision:
        operation.founder_report_json=None
        db.session.commit()
    return record
