"""Work-first budget authority for Company Core v0.18.

Project/Founder authority is the business boundary.  Work is the execution
boundary.  Operation remains a proposal/audit envelope for migrated flows, but
its kernel state is not consulted here.
"""
from __future__ import annotations

from decimal import Decimal
from sqlalchemy import func, or_

from ..extensions import db
from ..models import AgentRun, CostEvent, CostReservation, Work
from .company import remaining as company_remaining


class WorkBudgetRequired(ValueError):
    def __init__(self, approved, spent, additional, *, scope="PROJECT"):
        self.approved = Decimal(str(approved or 0))
        self.spent = Decimal(str(spent or 0))
        self.additional = Decimal(str(additional or 0))
        self.scope = scope
        super().__init__(
            f"{scope.title()} budget authority needs NT${self.additional} more "
            f"(authorized NT${self.approved}, spent NT${self.spent})."
        )


def _sum(**filters) -> Decimal:
    return Decimal(
        db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta), 0))
        .filter_by(**filters).scalar() or 0
    )


def _execution_sum(**filters) -> Decimal:
    query = (
        db.session.query(func.coalesce(func.sum(CostEvent.real_cost_delta), 0))
        .outerjoin(AgentRun, CostEvent.agent_run_id == AgentRun.id)
        .filter(or_(CostEvent.agent_run_id.is_(None), AgentRun.purpose != "CEO_FOUNDER_REQUEST"))
    )
    for name, value in filters.items():
        query = query.filter(getattr(CostEvent, name) == value)
    return Decimal(query.scalar() or 0)


def _active_reserved(*, project_id=None, work_id=None) -> Decimal:
    query = (
        db.session.query(func.coalesce(func.sum(CostReservation.estimated_twd), 0))
        .join(AgentRun, CostReservation.agent_run_id == AgentRun.id)
        .filter(CostReservation.status.in_(["RESERVED", "AMBIGUOUS"]))
    )
    if project_id is not None:
        query = query.filter(AgentRun.project_id == project_id)
    if work_id is not None:
        query = query.filter(AgentRun.work_id == work_id)
    return Decimal(query.scalar() or 0)


def snapshot(work: Work) -> dict:
    project = work.project
    operation = work.operation
    project_spent = _execution_sum(project_id=work.project_id)
    work_spent = _sum(work_id=work.id)
    operation_spent = _execution_sum(operation_id=operation.id) if operation else Decimal("0")
    project_reserved = _active_reserved(project_id=work.project_id)
    work_reserved = _active_reserved(work_id=work.id)
    company_reserved = _active_reserved()
    project_cap = None
    if project:
        contract = __import__(
            "eason_one.services.project_contract",
            fromlist=["is_vnext_governed", "assert_authority_ledger"],
        )
        if contract.is_vnext_governed(project):
            authority = contract.assert_authority_ledger(project)
            raw = authority.get("effective_budget_limit_twd")
            project_cap = Decimal(str(raw)) if raw not in (None, "") else None
        elif project.real_budget_limit is not None:
            project_cap = Decimal(project.real_budget_limit)
    operation_cap = Decimal(operation.approved_budget_twd) if operation and operation.approved_at is not None else None
    work_cap = Decimal(work.resource_ceiling_twd) if work.resource_ceiling_twd is not None else None
    return {
        "project_spent": project_spent,
        "work_spent": work_spent,
        "operation_spent": operation_spent,
        "project_reserved": project_reserved,
        "work_reserved": work_reserved,
        "company_reserved": company_reserved,
        "project_cap": project_cap,
        "operation_cap": operation_cap,
        "work_cap": work_cap,
        "company_remaining": Decimal(company_remaining()),
    }


def ensure(work: Work, estimated=Decimal("0"), *, include_reservations: bool = False) -> bool:
    estimated = max(Decimal("0"), Decimal(str(estimated or 0)))
    operation = work.operation
    snap = snapshot(work)
    company_available = snap["company_remaining"] - (snap["company_reserved"] if include_reservations else Decimal("0"))
    if company_available < 0 or estimated > company_available:
        raise WorkBudgetRequired(
            snap["company_remaining"], snap["company_reserved"] if include_reservations else Decimal("0"),
            estimated - company_available, scope="COMPANY",
        )

    # The Project is the Founder-owned hard envelope. A real provider dispatch
    # must count other in-flight Employee reservations, not only settled cost.
    if snap["project_cap"] is not None:
        project_reserved = snap["project_reserved"] if include_reservations else Decimal("0")
        available = snap["project_cap"] - snap["project_spent"] - project_reserved
        if available < 0 or estimated > available:
            raise WorkBudgetRequired(
                snap["project_cap"], snap["project_spent"] + project_reserved, estimated - available,
                scope="PROJECT",
            )

    core_v018 = bool(
        operation
        and __import__(
            "eason_one.services.core_v018", fromlist=["is_v018_operation"]
        ).is_v018_operation(operation)
    )
    if core_v018:
        # v0.18 hard authority is exactly the Founder-approved Project envelope.
        # Operation/Work estimates remain planning metadata and may not invent a
        # second Founder budget gate. Atomic reservations protect the Project
        # envelope itself; they do not promote Work estimates into authority.
        return True

    # Historical Work-first rows retain their original sub-envelope semantics.
    if snap["operation_cap"] is not None:
        available = snap["operation_cap"] - snap["operation_spent"]
        if available < 0 or estimated > available:
            raise WorkBudgetRequired(
                snap["operation_cap"], snap["operation_spent"], estimated - available,
                scope="MISSION",
            )

    if snap["work_cap"] is not None:
        work_reserved = snap["work_reserved"] if include_reservations else Decimal("0")
        available = snap["work_cap"] - snap["work_spent"] - work_reserved
        if available < 0 or estimated > available:
            raise WorkBudgetRequired(
                snap["work_cap"], snap["work_spent"], estimated - available,
                scope="WORK",
            )
    return True
