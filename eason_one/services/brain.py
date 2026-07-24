from ..extensions import db
from ..models import KnowledgeItem,KnowledgeReference
KINDS={"FACT","HYPOTHESIS","EVIDENCE","DECISION","CORRECTION","KILLED"}
def validate(kind, **kwargs):
    if kind not in KINDS: raise ValueError("Unknown knowledge kind")
    if kind=="EVIDENCE" and not kwargs.get("source_ref"): raise ValueError("Evidence requires source_ref")
    if kind=="DECISION" and not kwargs.get("rationale"): raise ValueError("Decision requires rationale")
    if kind in {"CORRECTION","KILLED"} and not kwargs.get("target_knowledge_id"): raise ValueError(f"{kind} requires a target")
    if kind in {"CORRECTION","KILLED"}:
        target=db.session.get(KnowledgeItem,kwargs["target_knowledge_id"])
        if not target: raise ValueError(f"{kind} target does not exist")
        if not target.founder_approved: raise ValueError(f"{kind} target must be Founder-approved")
        project_id=kwargs.get("project_id")
        if project_id is None and target.project_id is not None: raise ValueError("Company-level knowledge cannot target Project knowledge")
        if project_id is not None and target.project_id not in {None,project_id}: raise ValueError("Knowledge target belongs to an unrelated Project")
        if kind=="KILLED" and target.kind!="HYPOTHESIS": raise ValueError("KILLED target must be a HYPOTHESIS")
    return True
def build_knowledge(kind,title,content,**kwargs):
    kwargs.pop("basis_knowledge_ids",None)
    validate(kind,**kwargs); return KnowledgeItem(kind=kind,title=title,content=content,**kwargs)
def validate_basis_ids(ids,project_id):
    if not isinstance(ids,list) or len(set(ids))!=len(ids): raise ValueError("Invalid basis IDs")
    effective={item.id:item for item in current(project_id)}
    allowed={"FACT","HYPOTHESIS","EVIDENCE","CORRECTION"}
    rows=[]
    for ident in ids:
        item=db.session.get(KnowledgeItem,ident)
        if not item or not item.founder_approved: raise ValueError("Decision basis must be Founder-approved")
        if item.project_id not in {None,project_id}: raise ValueError("Decision basis belongs to an unrelated Project")
        if ident not in effective or item.kind not in allowed: raise ValueError("Decision basis must be currently effective FACT, HYPOTHESIS, EVIDENCE, or CORRECTION")
        rows.append(item)
    return rows
def add_knowledge(kind,title,content,**kwargs):
    basis=kwargs.pop("basis_knowledge_ids",[]) or []
    try:
        if kind=="DECISION": validate_basis_ids(basis,kwargs.get("project_id"))
        item=build_knowledge(kind,title,content,**kwargs); db.session.add(item); db.session.flush()
        if kind=="DECISION":
            for source in basis: db.session.add(KnowledgeReference(from_knowledge_id=source,to_knowledge_id=item.id,relation_type="BASIS_FOR"))
        db.session.commit(); return item
    except Exception: db.session.rollback(); raise
def current(project_id=None):
    q=KnowledgeItem.query.filter_by(founder_approved=True)
    if project_id is None: q=q.filter(KnowledgeItem.project_id.is_(None))
    else: q=q.filter((KnowledgeItem.project_id==project_id)|(KnowledgeItem.project_id.is_(None)))
    items=q.all(); superseded={x.target_knowledge_id for x in items if x.kind in {"CORRECTION","KILLED"}}
    return [x for x in items if x.id not in superseded]
def active_hypotheses(project_id=None):
    return [x for x in current(project_id) if x.kind=="HYPOTHESIS"]
