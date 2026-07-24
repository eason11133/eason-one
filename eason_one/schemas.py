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
    "required": ["mode", "executive_response", "project", "project_id", "tasks"],
    "properties": {
        "mode": {"type": "string", "enum": ["NEW_PROJECT", "PROJECT_ACTION", "STATUS_QUERY"]},
        "executive_response": {"type": "string"},
        "project": {"anyOf": [
            {"type": "object", "additionalProperties": False, "required": ["name", "objective", "priority"],
             "properties": {"name": {"type": "string"}, "objective": {"type": "string"},
                            "priority": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"]}}},
            {"type": "null"},
        ]},
        "project_id": {"type": ["integer", "null"]},
        "tasks": {"type": "array", "maxItems": 12, "items": TASK_FIELDS},
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
