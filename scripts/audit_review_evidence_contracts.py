#!/usr/bin/env python3
"""Offline structural acceptance for review/evidence ownership and actor locality."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def read(rel):
    return (ROOT / rel).read_text(encoding='utf-8')

def require(ok, msg):
    if not ok:
        raise AssertionError(msg)
    print(' - PASS:', msg)

work = read('eason_one/services/work_execution.py')
recovery = read('eason_one/services/runtime_recovery.py')
outcome = read('eason_one/services/project_outcome.py')
kernel = read('eason_one/services/company_kernel.py')
context = read('eason_one/services/context.py')
policy = read('eason_one/services/execution_policy.py')

require('WORK_REVIEW_V4_ARTIFACT_SOURCE_LINEAGE' in work,
        'Work semantic review uses the Artifact/source-lineage protocol')
require('select_research_model(reviewer' not in work and 'LIVE_WEB_RESEARCH_REVIEW' not in work,
        'Research Work review does not silently replace the frozen reviewer with a research provider')
require('model = select_execution_model(reviewer, work.operation, "TASK_REVIEW")' in work,
        'Frozen reviewer Employee owns TASK_REVIEW execution policy')
require('reviewed_artifact_source_lineage' in work and '_artifact_provider_source_lineage(version)' in work,
        'Research review consumes provider sources from the exact producing ArtifactVersion')
require('RESEARCH_ARTIFACT_SOURCE_LINEAGE_MISSING' in work,
        'Missing producer source lineage fails closed before a paid reviewer call')
require('RESEARCH_REVIEW_SOURCE_EVIDENCE_MISSING' not in work,
        'Reviewer call is no longer required to emit a redundant second source list')
require('SYSTEM_RESEARCH_REVIEW_EVIDENCE_SCOPE_SUPERSEDED' in work and
        'SYSTEM_RESEARCH_REVIEW_EVIDENCE_SCOPE_SUPERSEDED' in recovery,
        'Historical mis-scoped review failure is auditable and does not consume business retry quota')
require('reconcile_research_review_evidence_scope_faults' in recovery and
        'implementation_replayed": False' in recovery and 'new_provider_call": False' in recovery,
        'Platform recovery resumes review without replaying Research or spending during reconciliation')
require('provider_sources' in outcome and 'input_artifact_lineage' in outcome and
        'Provider-observed source lineage for this accepted ArtifactVersion' in outcome,
        'Project outcome packet preserves accepted provider/source and handoff lineage')
require('CRITICAL_REVIEW' in outcome and 'select_outcome_reviewer' in outcome and
        'PROJECT_OUTCOME_REVIEW' in kernel,
        'Final Project outcome review remains assigned to an independent critical reviewer')
require('provider_sources' in context and 'artifact_content_hash' in context,
        'Downstream Work context carries source lineage bound to accepted Artifact hashes')
require('_CLAUDE_RESERVED_EMPLOYEE_SLUGS = {"critic"}' in policy,
        'Formal Critic remains the Claude-reserved review role')

print('REVIEW_EVIDENCE_CONTRACT_AUDIT_PASS')
