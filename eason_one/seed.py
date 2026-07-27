import click
from .extensions import db
from .models import Company, Department, Position, ModelConfig, Employee, EmployeeModelHistory

def ensure_hr():
    from .services.workforce import HR_ASSESSMENT_OUTPUT_CAP
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
          ModelConfig.provider_key!="mock",
          ModelConfig.max_output_tokens>=HR_ASSESSMENT_OUTPUT_CAP)
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
          or employee.current_model.max_output_tokens<HR_ASSESSMENT_OUTPUT_CAP):
        available=ModelConfig.query.filter_by(active=True,archived=False).filter(
          ModelConfig.provider_key!="mock",
          ModelConfig.max_output_tokens>=HR_ASSESSMENT_OUTPUT_CAP)
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

def seed():
    if Company.query.first():
        ensure_hr()
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
    db.session.commit(); ensure_hr(); return company

@click.command("seed")
def seed_command():
    seed(); click.echo("Eason One seeded.")
