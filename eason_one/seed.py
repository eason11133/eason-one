import click
from .extensions import db
from .models import Company, Department, Position, ModelConfig, Employee, EmployeeModelHistory

def seed():
    if Company.query.first(): return Company.query.first()
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
    db.session.commit(); return company

@click.command("seed")
def seed_command():
    seed(); click.echo("Eason One seeded.")
