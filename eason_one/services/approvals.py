from ..extensions import db
from ..models import now
from .brain import build_knowledge,validate_basis_ids
from ..models import KnowledgeReference
def review_proposal(proposal, decision, note=None, corrected=None):
    if proposal.status!="PENDING" or decision not in {"APPROVED","REJECTED","CORRECTED"}: raise ValueError("Invalid proposal review")
    try:
        if decision in {"APPROVED","CORRECTED"}:
            p=corrected if decision=="CORRECTED" else proposal.payload_json
            p=dict(p); basis=p.pop("basis_knowledge_ids",[]) or []
            if p.get("kind")=="DECISION": validate_basis_ids(basis,proposal.project_id)
            item=build_knowledge(project_id=proposal.project_id,founder_approved=True,origin_agent_run_id=proposal.agent_run_id,
                origin_employee_id=proposal.proposed_by_employee_id,**p)
            db.session.add(item); db.session.flush()
            if p.get("kind")=="DECISION":
                for source in basis: db.session.add(KnowledgeReference(from_knowledge_id=source,to_knowledge_id=item.id,relation_type="BASIS_FOR"))
            proposal.materialized_knowledge_id=item.id
        proposal.status=decision; proposal.review_note=note; proposal.reviewed_at=now()
        if corrected: proposal.corrected_payload_json=corrected
        db.session.commit(); return proposal
    except Exception:
        db.session.rollback(); raise
