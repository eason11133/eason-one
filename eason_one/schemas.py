TASK_FIELDS = {
    "type": "object", "additionalProperties": False,
    "required": ["title", "objective", "assignee_slug", "reviewer_slug", "required_output", "acceptance_criteria"],
    "properties": {
        "title": {"type": "string"}, "objective": {"type": "string"},
        "assignee_slug": {"type": "string"}, "reviewer_slug": {"type": ["string", "null"]},
        "required_output": {"type": "string"}, "acceptance_criteria": {"type": "string"},
    },
}
CEO_SCHEMA = {"name": "ceo_founder_request", "schema": {
    "type": "object", "additionalProperties": False,
    "required": ["mode", "executive_response", "project", "project_id", "tasks", "operation"],
    "properties": {
        "mode": {"type": "string", "enum": ["NEW_PROJECT", "PROJECT_ACTION", "STATUS_QUERY", "ADVISORY", "OPERATION_PLAN", "OPERATION_FOLLOW_UP"]},
        "executive_response": {"type": "string", "maxLength": 220},
        "project": {"anyOf": [
            {"type": "object", "additionalProperties": False, "required": ["name", "objective", "priority"],
             "properties": {"name": {"type": "string"}, "objective": {"type": "string"},
                            "priority": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"]}}},
            {"type": "null"},
        ]},
        "project_id": {"type": ["integer", "null"]},
        "tasks": {"type": "array", "maxItems": 12, "items": TASK_FIELDS},
        "operation": {"anyOf": [
            {"type": "object", "additionalProperties": False,
             "required": ["title", "objective", "project_id", "budget_twd", "tasks",
                          "meeting_policy", "completion_criteria"],
             "properties": {
                 "title": {"type": "string", "maxLength": 80},
                 "objective": {"type": "string", "maxLength": 180},
                 "project_id": {"type": ["integer", "null"]},
                 "budget_twd": {"type": "number", "exclusiveMinimum": 0},
                 "tasks": {"type": "array", "minItems": 1, "maxItems": 6, "items": {
                     "type": "object", "additionalProperties": False,
                     "required": ["title", "objective", "assignee_employee_id",
                                  "reviewer_employee_id", "acceptance_criteria"],
                     "properties": {
                         "title": {"type": "string", "maxLength": 70},
                         "objective": {"type": "string", "maxLength": 140},
                         "assignee_employee_id": {"type": "integer"},
                         "reviewer_employee_id": {"type": "integer"},
                         "acceptance_criteria": {"type": "array", "minItems": 1,
                                                "maxItems": 2,
                                                "items": {"type": "string",
                                                          "maxLength": 120}},
                     },
                 }},
                 "meeting_policy": {"type": "string", "maxLength": 80},
                 "completion_criteria": {"type": "array", "minItems": 1,
                                         "maxItems": 6,
                                         "items": {"type": "string",
                                                   "maxLength": 120}},
             }},
            {"type": "null"},
        ]},
    },
}}
REVIEW_SCHEMA = {"name": "task_review", "schema": {
    "type": "object", "additionalProperties": False,
    "required": ["decision", "summary", "issues", "required_changes"],
    "properties": {
        "decision": {"type": "string", "enum": ["ACCEPT", "REVISE", "BLOCK"]},
        "summary": {"type": "string"}, "issues": {"type": "array", "items": {"type": "string"}},
        "required_changes": {"type": "array", "items": {"type": "string"}},
    },
}}
SYNTHESIS_SCHEMA = {"name": "ceo_project_synthesis", "schema": {
    "type": "object", "additionalProperties": False,
    "required": ["executive_summary", "result", "key_findings", "disagreements_or_risks",
                 "unresolved_questions", "founder_decisions_required", "recommended_next_actions"],
    "properties": {
        "executive_summary": {"type": "string"}, "result": {"type": "string"},
        "key_findings": {"type": "array", "items": {"type": "string"}},
        "disagreements_or_risks": {"type": "array", "items": {"type": "string"}},
        "unresolved_questions": {"type": "array", "items": {"type": "string"}},
        "founder_decisions_required": {"type": "array", "items": {"type": "string"}},
        "recommended_next_actions": {"type": "array", "items": {"type": "string"}},
    },
}}

CEO_DECISION_SCHEMA = {"name": "ceo_operation_decision", "schema": {
    "type": "object", "additionalProperties": False,
    "required": ["action", "reason", "goal_status", "confidence",
                 "next_task_id", "meeting", "hiring_need", "founder_request",
                 "task_plan"],
    "properties": {
        "action": {"type": "string", "enum": [
            "CONTINUE", "MEETING", "HIRING_REQUEST", "FOUNDER", "COMPLETE",
            "CREATE_TASK", "REASSIGN_TASK",
        ]},
        "reason": {"type": "string"}, "goal_status": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "next_task_id": {"type": ["integer", "null"]},
        "meeting": {"anyOf": [
            {"type": "object", "additionalProperties": False,
             "required": ["question", "budget_twd"],
             "properties": {"question": {"type": "string"},
                            "budget_twd": {"type": "number", "exclusiveMinimum": 0}}},
            {"type": "null"},
        ]},
        "hiring_need": {"anyOf": [
            {"type": "object", "additionalProperties": False,
             "required": ["role_needed", "problem", "why_now",
                          "responsibilities", "capabilities", "urgency",
                          "use_frequency"],
             "properties": {
                 "role_needed": {"type": "string"}, "problem": {"type": "string"},
                 "why_now": {"type": "string"},
                 "responsibilities": {"type": "array", "items": {"type": "string"}},
                 "capabilities": {"type": "array", "items": {"type": "string"}},
                 "urgency": {"type": "string"},
                 "use_frequency": {"type": "string"},
             }},
            {"type": "null"},
        ]},
        "founder_request": {"type": ["string", "null"]},
        "task_plan": {"anyOf": [
            {"type": "object", "additionalProperties": False,
             "required": ["task_id", "title", "objective",
                          "assignee_employee_id", "reviewer_employee_id",
                          "acceptance_criteria", "reason"],
             "properties": {
                 "task_id": {"type": ["integer", "null"]},
                 "title": {"type": ["string", "null"]},
                 "objective": {"type": ["string", "null"]},
                 "assignee_employee_id": {"type": "integer"},
                 "reviewer_employee_id": {"type": "integer"},
                 "acceptance_criteria": {
                     "type": "array", "items": {"type": "string"},
                 },
                 "reason": {"type": "string"},
             }},
            {"type": "null"},
        ]},
    },
}}

HR_ASSESSMENT_SCHEMA = {"name": "hr_hiring_assessment", "schema": {
    "type": "object", "additionalProperties": False,
    "required": ["recommendation", "existing_staff_alternative",
                 "need_duration", "duplicate_capability",
                 "candidate_template_id", "target_department_id",
                 "target_position", "manager_employee_id",
                 "recommended_model_config_id", "estimated_input_tokens",
                 "estimated_output_tokens", "expected_calls_per_mission",
                 "missions_per_month", "max_mission_budget_twd",
                 "expected_benefit", "redundancy_risk", "alternatives",
                 "success_criteria", "probation_assignments", "instructions"],
    "properties": {
        "recommendation": {"type": "string", "enum": [
            "HIRE", "DO NOT HIRE", "TEMPORARY", "USE EXISTING STAFF"
        ]},
        "existing_staff_alternative": {"type": "string"},
        "need_duration": {"type": "string"},
        "duplicate_capability": {"type": "string"},
        "candidate_template_id": {"type": ["integer", "null"]},
        "target_department_id": {"type": "integer"},
        "target_position": {"type": "string"},
        "manager_employee_id": {"type": "integer"},
        "recommended_model_config_id": {"type": "integer"},
        "estimated_input_tokens": {"type": "integer", "minimum": 0},
        "estimated_output_tokens": {"type": "integer", "minimum": 0},
        "expected_calls_per_mission": {"type": "integer", "minimum": 1},
        "missions_per_month": {"type": "integer", "minimum": 0},
        "max_mission_budget_twd": {"type": "number", "minimum": 0},
        "expected_benefit": {"type": "string"},
        "redundancy_risk": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
        "alternatives": {"type": "array", "items": {"type": "string"}},
        "success_criteria": {"type": "array", "items": {"type": "string"}},
        "probation_assignments": {"type": "integer", "minimum": 1},
        "instructions": {"type": "string"},
    },
}}

GOAL_VERIFICATION_SCHEMA = {"name": "operation_goal_verification", "schema": {
    "type": "object", "additionalProperties": False,
    "required": [
        "overall_status", "criteria", "summary", "recommended_action",
    ],
    "properties": {
        "overall_status": {"type": "string", "enum": [
            "SATISFIED", "NOT_SATISFIED", "INSUFFICIENT_EVIDENCE",
        ]},
        "criteria": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["criterion", "status", "evidence", "reason"],
                "properties": {
                    "criterion": {"type": "string"},
                    "status": {"type": "string", "enum": [
                        "SATISFIED", "NOT_SATISFIED",
                        "INSUFFICIENT_EVIDENCE",
                    ]},
                    "evidence": {
                        "type": "array", "items": {"type": "string"},
                    },
                    "reason": {"type": "string"},
                },
            },
        },
        "summary": {"type": "string"},
        "recommended_action": {"type": "string"},
    },
}}

KNOWLEDGE_CANDIDATE = {
    "type": "object", "additionalProperties": False,
    "required": ["kind", "title", "content", "source_ref", "rationale", "basis_knowledge_ids"],
    "properties": {
        "kind": {"type": "string", "enum": ["FACT", "HYPOTHESIS", "EVIDENCE", "DECISION"]},
        "title": {"type": "string"}, "content": {"type": "string"},
        "source_ref": {"type": ["string", "null"]}, "rationale": {"type": ["string", "null"]},
        "basis_knowledge_ids": {"type": "array", "items": {"type": "integer"}},
    },
}
TASK_EXECUTION_SCHEMA = {"name": "task_execution", "schema": {
    "type": "object", "additionalProperties": False,
    "required": ["result_summary", "knowledge_proposals"],
    "properties": {
        "result_summary": {"type": "string"},
        "knowledge_proposals": {"type": "array", "maxItems": 8, "items": KNOWLEDGE_CANDIDATE},
    },
}}

MEETING_SYNTHESIS_FIELDS = [
    "agreements", "disagreements", "evidence_referenced",
    "rejected_or_unresolved", "actions", "founder_decisions_required",
]
MEETING_SYNTHESIS_SCHEMA = {"name": "meeting_synthesis", "schema": {
    "type": "object", "additionalProperties": False,
    "required": MEETING_SYNTHESIS_FIELDS,
    "properties": {
        field: {"type": "array", "maxItems": 8, "items": {"type": "string", "maxLength": 500}}
        for field in MEETING_SYNTHESIS_FIELDS
    },
}}

MEETING_ROUND1_SCHEMA = {"name":"meeting_round1_contribution","schema":{
    "type":"object","additionalProperties":False,
    "required":["position","actions","risk","evidence_ids","confidence"],
    "properties":{
        "position":{"type":"string","maxLength":180},
        "actions":{"type":"array","maxItems":3,"items":{"type":"string","maxLength":120}},
        "risk":{"type":"string","maxLength":180},
        "evidence_ids":{"type":"array","maxItems":8,"items":{"type":"integer"}},
        "confidence":{"type":"number","minimum":0,"maximum":1},
    },
}}
MEETING_LATER_SCHEMA = {"name":"meeting_later_contribution","schema":{
    "type":"object","additionalProperties":False,
    "required":["has_material_contribution","type","core_point","controls","risk","evidence_ids","target_message_ids"],
    "properties":{
        "has_material_contribution":{"type":"boolean"},
        "type":{"type":"string","enum":["DISAGREEMENT","COUNTEREVIDENCE","NEW_INFORMATION","MATERIAL_REFINEMENT"]},
        "core_point":{"type":"string","maxLength":180},
        "controls":{"type":"array","maxItems":3,"items":{"type":"string","maxLength":120}},
        "risk":{"type":"string","maxLength":180},
        "evidence_ids":{"type":"array","maxItems":8,"items":{"type":"integer"}},
        "target_message_ids":{"type":"array","maxItems":8,"items":{"type":"integer"}},
    },
}}
MEETING_RESPONSE_SCHEMA = {"name":"meeting_same_round_response","schema":{
    "type":"object","additionalProperties":False,
    "required":["relation","core_point","controls","risk","evidence_ids"],
    "properties":{
        "relation":{"type":"string","enum":["AGREE","DISAGREE","ADD_INFORMATION","QUALIFY"]},
        "core_point":{"type":"string","maxLength":180},
        "controls":{"type":"array","maxItems":3,"items":{"type":"string","maxLength":120}},
        "risk":{"type":"string","maxLength":180},
        "evidence_ids":{"type":"array","maxItems":8,"items":{"type":"integer"}},
    },
}}
MEETING_ROUTER_SCHEMA = {"name":"meeting_chair_router","schema":{
    "type":"object","additionalProperties":False,
    "required":["continue_meeting","next_speakers","reason","founder_input_required","founder_question"],
    "properties":{
        "continue_meeting":{"type":"boolean"},
        "next_speakers":{"type":"array","maxItems":4,"items":{"type":"string"}},
        "reason":{"type":"string","maxLength":240},
        "founder_input_required":{"type":"boolean"},
        "founder_question":{"type":["string","null"],"maxLength":240},
    },
}}
