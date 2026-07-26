from ..extensions import db
from ..models import EmployeeModelHistory, now

def change_model(employee, model_config, reason=None):
    if model_config and (not model_config.active or model_config.archived):
        raise ValueError("ModelConfig must be active and not archived")
    current = EmployeeModelHistory.query.filter_by(employee_id=employee.id, ended_at=None).first()
    if current:
        current.ended_at = now()
    employee.current_model = model_config
    db.session.add(EmployeeModelHistory(employee_id=employee.id, model_config_id=model_config.id if model_config else None, reason=reason))
    db.session.commit()
    return employee
