import click
from .extensions import db
from .models import Company, Department, Position, ModelConfig, Employee, EmployeeModelHistory



def ensure_current_org_shape():
    """Apply the current intentionally-flat Engineering org without deleting history.

    There is one active Engineer today, so a synthetic Engineering Director is
    organizational noise and an unnecessary routing/review bottleneck. Preserve
    the historical Employee row for old Projects/tests/audit, but remove it from
    active staffing and make the Engineer report directly to the CEO.
    """
    ceo = Employee.query.filter_by(slug="ceo").first()
    engineer = Employee.query.filter_by(slug="engineer").first()
    director = Employee.query.filter_by(slug="engineering-director").first()
    changed = False
    if director and director.active:
        director.active = False
        director.employment_status = "INACTIVE"
        changed = True
    if engineer and ceo and engineer.manager_id != ceo.id:
        engineer.manager_id = ceo.id
        changed = True
    if changed:
        db.session.flush()
    return changed

def ensure_hr():
    company=Company.query.first()
    if not company: return None
    ceo_office=Department.query.filter_by(name="CEO Office").first()
    if not ceo_office:
        ceo_office=Department(
          name="CEO Office",
          description="Founder interface, executive coordination, and assurance.")
        db.session.add(ceo_office); db.session.flush()
    ceo=Employee.query.filter_by(slug="ceo").first()
    if ceo and ceo.department_id is None:
        ceo.department_id=ceo_office.id
    for executive in Employee.query.filter(
      Employee.department_id.is_(None)).all():
        if executive.slug in {"critic"}:
            executive.department_id=ceo_office.id
    department=Department.query.filter_by(name="Human Resources").first()
    if not department:
        department=Department(name="Human Resources",
          description="Workforce planning, hiring review, candidate management, and role governance.")
        db.session.add(department); db.session.flush()
    position=Position.query.filter_by(name="HR Director").first()
    if not position:
        position=Position(name="HR Director",level=3,
          description="Leads governed workforce planning and challenges staffing requests.")
        db.session.add(position); db.session.flush()
    employee=Employee.query.filter_by(slug="hr-director").first()
    if not employee:
        available=ModelConfig.query.filter_by(active=True,archived=False).filter(
          ModelConfig.provider_key.in_(["openai","anthropic","gemini","perplexity"]),
          ModelConfig.max_output_tokens>0)
        luna=(available.filter(
          db.func.lower(ModelConfig.label).like("%gpt-5.6%luna%")).first()
          or available.first()
          or ModelConfig.query.filter_by(
            provider_key="mock",active=True,archived=False).first())
        employee=Employee(name="HR Director",slug="hr-director",
          department_id=department.id,position_id=position.id,
          manager_id=getattr(ceo,"id",None),
          role_description="Workforce planning, hiring review, candidate management, and role duplication review.",
          system_instructions=("Challenge every staffing request. Compare existing staff, temporary coverage, "
            "duplication, model sufficiency, cost, expected benefit, and measurable probation criteria."),
          current_model_config_id=getattr(luna,"id",None),salary_credits_per_week=0)
        db.session.add(employee); db.session.flush()
        if luna:
            db.session.add(EmployeeModelHistory(employee_id=employee.id,
              model_config_id=luna.id,reason="Initial HR Director assignment"))
    elif (not employee.current_model or not employee.current_model.active
          or employee.current_model.archived
          or employee.current_model.provider_key=="mock"
          or int(employee.current_model.max_output_tokens or 0)<=0):
        available=ModelConfig.query.filter_by(active=True,archived=False).filter(
          ModelConfig.provider_key.in_(["openai","anthropic","gemini","perplexity"]),
          ModelConfig.max_output_tokens>0)
        model=(available.filter(
          db.func.lower(ModelConfig.label).like("%gpt-5.6%luna%")).first()
          or available.first()
          or ModelConfig.query.filter_by(
            provider_key="mock",active=True,archived=False).first())
        if model:
            employee.current_model_config_id=model.id
            db.session.add(EmployeeModelHistory(employee_id=employee.id,
              model_config_id=model.id,reason="Restored operational HR assessment model"))
    employee.active=True
    employee.employment_status="ACTIVE"
    db.session.commit()
    return employee



def ensure_research_department_roster():
    """Materialize the persistent model-specialized Research Department roster.

    Existing Project/Work assignments are never rewritten here. The historical
    generic Researcher remains active for compatibility with already-approved
    Work while new provider-family specialists become available for future staffing.
    Exact model versions may change; Employee identity remains stable.
    """
    company=Company.query.first()
    if not company:
        return []
    ceo=Employee.query.filter_by(slug="ceo").first()
    department=Department.query.filter_by(name="Research Department").first()
    if not department:
        department=Department(name="Research Department",
          description="Persistent model-specialized research, evidence comparison, and accountable department synthesis.")
        db.session.add(department); db.session.flush()
    director_position=Position.query.filter_by(name="Director").first()
    if not director_position:
        director_position=Position(name="Director",level=3,description="Department leadership and accountable synthesis.")
        db.session.add(director_position); db.session.flush()
    researcher_position=Position.query.filter_by(name="Researcher").first()
    if not researcher_position:
        researcher_position=Position(name="Researcher",level=1,
          description="Persistent specialist researcher with durable memory and provider-family specialization.")
        db.session.add(researcher_position); db.session.flush()
    director=Employee.query.filter_by(slug="research-director").first()
    if not director:
        director=Employee(name="Research Director",slug="research-director",
          department_id=department.id,position_id=director_position.id,manager_id=getattr(ceo,"id",None),
          role_description="Lead Research Department staffing, evidence reconciliation, and accountable synthesis.",
          system_instructions=("RESEARCH_DEPARTMENT_HEAD_V1. Lead a persistent Research Department. Delegate ordinary "
            "research to eligible provider-specialized Researchers; synthesize accepted independent outputs; preserve "
            "material disagreement; never invent sources, capability, authority, or budget."),
          current_model_config_id=getattr(getattr(ceo,"current_model",None),"id",None),salary_credits_per_week=500)
        db.session.add(director); db.session.flush()
    else:
        director.department_id=department.id
        director.manager_id=getattr(ceo,"id",None)
        if director.position_id is None:
            director.position_id=director_position.id
        if director.role_description.strip()=="Plan and review research.":
            director.role_description="Lead Research Department staffing, evidence reconciliation, and accountable synthesis."
        marker="RESEARCH_DEPARTMENT_HEAD_V1"
        if marker not in (director.system_instructions or ""):
            director.system_instructions=((director.system_instructions or director.role_description).rstrip()+
              "\n"+marker+". Delegate ordinary research to eligible provider-specialized Researchers; synthesize accepted independent outputs; preserve material disagreement; never invent sources, capability, authority, or budget.")
        director.active=True
        director.employment_status="ACTIVE"
    # Preserve the old generic Researcher and its Project history; only make sure
    # its reporting line stays under the Research Director.
    legacy=Employee.query.filter_by(slug="researcher").first()
    if legacy and legacy.manager_id!=director.id:
        legacy.manager_id=director.id

    policy=__import__("eason_one.services.research_department",fromlist=["SPECIALISTS"])
    created=[]
    for spec in policy.SPECIALISTS:
        models=(ModelConfig.query.filter_by(provider_key=spec["provider"],active=True,archived=False)
          .filter(ModelConfig.max_output_tokens>0).all())
        if not models:
            # Do not fabricate a provider/model binding. The role will materialize
            # automatically on a later normal startup once a real ModelConfig exists.
            continue
        models=sorted(models,key=lambda row:(
          (row.input_price_per_million or 0)+(row.output_price_per_million or 0)*2+(row.request_price_per_call or 0),row.id))
        preferred=models[0]
        employee=Employee.query.filter_by(slug=spec["slug"]).first()
        if not employee:
            employee=Employee(name=spec["name"],slug=spec["slug"],department_id=department.id,
              position_id=researcher_position.id,manager_id=director.id,
              role_description=(f"Persistent {spec['label']} research specialist. Owns accountable Research Work, durable memory, "
                "and EC compensation while using the provider family as a replaceable execution core."),
              system_instructions=(f"RESEARCH_SPECIALIST_V1 provider_family={spec['provider']}. Work as the persistent {spec['label']} "
                "Researcher. Keep your own accountable conclusion distinct from sibling Researchers. Never claim live sources unless "
                "runtime supplies provider-observed evidence. Provider/model identity never expands Project authority, capability, or budget."),
              current_model_config_id=preferred.id,salary_credits_per_week=500,active=True,employment_status="ACTIVE")
            db.session.add(employee); db.session.flush()
            db.session.add(EmployeeModelHistory(employee_id=employee.id,model_config_id=preferred.id,
              reason=f"Initial {spec['label']} Researcher provider-family assignment"))
            created.append(employee)
        else:
            employee.name=spec["name"]
            employee.department_id=department.id
            employee.position_id=researcher_position.id
            employee.manager_id=director.id
            current=employee.current_model
            if current is None or current.provider_key!=spec["provider"] or not current.active or current.archived:
                employee.current_model_config_id=preferred.id
                db.session.add(EmployeeModelHistory(employee_id=employee.id,model_config_id=preferred.id,
                  reason=f"Restored {spec['label']} provider-family specialization"))
            employee.active=True
            employee.employment_status="ACTIVE"
    db.session.flush()
    return created

def seed():
    if Company.query.first():
        ensure_hr()
        ensure_current_org_shape()
        ensure_research_department_roster()
        db.session.commit()
        return Company.query.first()
    company=Company(name="Eason One",real_budget_limit=3000,currency="TWD")
    research=Department(name="Research Department"); engineering=Department(name="Engineering Department")
    ceop=Position(name="CEO",level=4); director=Position(name="Director",level=3); employee=Position(name="Employee",level=1)
    mock=ModelConfig(label="Local Mock",provider_key="mock",model_name="deterministic-mock",
        input_price_per_million=0,output_price_per_million=0,currency="TWD")
    db.session.add_all([company,research,engineering,ceop,director,employee,mock]); db.session.flush()
    ceo=Employee(name="CEO",slug="ceo",position_id=ceop.id,role_description="Chief Coordinator / Executive Interface / Company Synthesizer",
        system_instructions="Be a concise executive. Propose actions; never mutate authoritative state.",current_model_config_id=mock.id,salary_credits_per_week=1000)
    db.session.add(ceo); db.session.flush()
    rows=[
      ("Research Director","research-director",research,director,ceo,"Plan and review research."),
      ("Researcher","researcher",research,employee,None,"Gather evidence and distinguish Company Brain facts, model reasoning/prior knowledge, and claims requiring external verification. Never fabricate web research or citations."),
      ("Engineering Director","engineering-director",engineering,director,ceo,"Coordinate and review engineering."),
      ("Engineer","engineer",engineering,employee,None,"Analyze and implement scoped engineering work."),
      ("Critic","critic",None,employee,ceo,"Perform independent adversarial review and surface blockers."),
    ]
    people=[ceo]
    for name,slug,dept,pos,manager,role in rows:
        e=Employee(name=name,slug=slug,department_id=getattr(dept,"id",None),position_id=pos.id,
          manager_id=getattr(manager,"id",None),role_description=role,system_instructions=role+" Do not change company policy.",
          current_model_config_id=mock.id,salary_credits_per_week=500)
        db.session.add(e); db.session.flush(); people.append(e)
    people[2].manager_id=people[1].id; people[4].manager_id=people[3].id
    for person in people: db.session.add(EmployeeModelHistory(employee_id=person.id,model_config_id=mock.id,reason="Initial assignment"))
    db.session.commit(); ensure_hr(); ensure_current_org_shape(); ensure_research_department_roster(); db.session.commit(); return company

@click.command("seed")
def seed_command():
    seed(); click.echo("Eason One seeded.")
