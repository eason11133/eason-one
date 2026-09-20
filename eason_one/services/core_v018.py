"""Eason One v0.18 Core Cutover markers and ownership helpers.

The v0.18 execution lane is intentionally explicit. Historical ``WORK_VNEXT``
Operations remain readable/auditable but are not eligible for scheduling unless
one safe latest candidate is deliberately migrated by Company Runtime.
"""
from __future__ import annotations

RUNTIME_SEMANTICS = "WORK_CORE_V018"
LEGACY_WORK_RUNTIME_SEMANTICS = "WORK_VNEXT"


def runtime_semantics(operation) -> str | None:
    if operation is None:
        return None
    return (operation.memory_json or {}).get("runtime_semantics")


def is_v018_declared_operation(operation) -> bool:
    """Return whether the Operation was created for the v0.18 control lane."""
    return bool(operation and runtime_semantics(operation) == RUNTIME_SEMANTICS)


def is_v018_operation(operation) -> bool:
    return bool(
        operation
        and operation.approved_at is not None
        and is_v018_declared_operation(operation)
    )


def is_retired_work_vnext(operation) -> bool:
    return bool(
        operation
        and operation.approved_at is not None
        and runtime_semantics(operation) == LEGACY_WORK_RUNTIME_SEMANTICS
    )


def is_dormant_legacy_project_operation(operation) -> bool:
    """Return whether a LIVE Project Operation predates the v0.18 control lane.

    After the Core Cutover, every current/future Project Operation is stamped
    WORK_CORE_V018 at proposal creation. Any LIVE Project Operation without that
    stamp is therefore historical, whether its old row says PLANNED, RUNNING or
    WAITING_FOR_FOUNDER. It may be read for audit but may not regain authority.
    """
    if not operation or is_v018_declared_operation(operation):
        return False
    project = getattr(operation, "project", None)
    return bool(project is not None and getattr(project, "environment", None) == "LIVE")


def bypass_legacy_operation_runtime(operation) -> bool:
    """Return True when OperationKernel/OperationRuntime must not execute it.

    v0.18 Work is owned by Company Runtime. Any older LIVE Project Operation
    is dormant audit history, regardless of which pre-v0.18 runtime
    semantics it used. Non-Project/system compatibility Operations may still
    use the legacy worker.
    """
    return bool(
        is_v018_operation(operation)
        or is_retired_work_vnext(operation)
        or is_dormant_legacy_project_operation(operation)
    )


def stamp_v018(operation) -> None:
    memory = dict(operation.memory_json or {})
    memory["runtime_semantics"] = RUNTIME_SEMANTICS
    memory["core_cutover_version"] = "0.18.0"
    operation.memory_json = memory
