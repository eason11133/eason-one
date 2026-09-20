"""Classification for legacy Proposal rows at the Founder boundary.

Proposal is a generic collaboration/inbox object.  It becomes Founder attention
only for the narrow pre-materialization PROJECT_PLAN authority path. Evidence,
knowledge, analysis and other proposals remain ordinary Company Inbox truth.
"""
from __future__ import annotations

from ..extensions import db
from ..models import Operation, Project, Proposal


def proposal_type(proposal: Proposal) -> str:
    return str((proposal.payload_json or {}).get("type") or "").strip().upper()


def is_initial_project_proposal(proposal: Proposal) -> bool:
    """PROJECT_PLAN rows are historical audit evidence only.

    v0.20 has one Project creation authority path: governed CEO OPERATION_PLAN
    approval. No Proposal row may become current Founder attention.
    """
    return False

def pending_initial_count(*, project_id: int | None = None) -> int:
    query = Proposal.query.filter_by(status="PENDING")
    if project_id is not None:
        query = query.filter_by(project_id=project_id)
    return sum(1 for row in query.all() if is_initial_project_proposal(row))
