from dataclasses import dataclass
import json
import os
@dataclass
class ProviderResult:
    text: str
    input_tokens: int
    output_tokens: int
    response_id: str|None=None
    request_id: str|None=None
    status: str="completed"
    refusal: str|None=None
    incomplete_reason: str|None=None
class MockProvider:
    def complete(self, model_config, system_prompt, user_prompt, context, max_output_tokens, response_schema=None):
        if "TASK_REVIEW" in system_prompt:
            decision="REVISE" if "revise" in user_prompt.lower() else ("BLOCK" if "block" in user_prompt.lower() else "ACCEPT")
            text=json.dumps({"decision":decision,"summary":"Mock reviewer assessed the result against acceptance criteria.",
                "issues":[] if decision=="ACCEPT" else ["A material issue remains"],"required_changes":[] if decision=="ACCEPT" else ["Address the identified issue"]})
        elif "MEETING_SYNTHESIS" in system_prompt:
            text=json.dumps({"agreements":["Use the smallest bounded next action."],
              "disagreements":["Evidence sufficiency remains disputed."],
              "evidence_referenced":["Only evidence recorded in the Meeting context was considered."],
              "rejected_or_unresolved":["External validation remains unresolved."],
              "actions":["Founder reviews and authorizes any next execution."],
              "founder_decisions_required":["Approve, revise, or stop the proposed next action."]})
        elif "CEO_PROJECT_SYNTHESIS" in system_prompt:
            text=json.dumps({"executive_summary":"The team completed a reviewable project cycle.","result":"Available results support a bounded Founder decision.",
              "key_findings":["Completed work and reviewer evidence are recorded"],"disagreements_or_risks":["Mock evidence is not market evidence"],
              "unresolved_questions":["Real customer validation remains"],"founder_decisions_required":["Choose whether to proceed to a paid test"],
              "recommended_next_actions":["Run the smallest authorized real-world validation"]})
        elif "CEO_FOUNDER_REQUEST" in system_prompt:
            status=any(x in user_prompt.lower() for x in ("how is","status","progress"))
            action=any(x in user_prompt.lower() for x in ("add task","next action","continue project"))
            project_ids=__import__("re").findall(r"Project #(\d+)",context)
            if status:
                text=json.dumps({"mode":"STATUS_QUERY","executive_response":"The requested project status is summarized from current operating state.",
                    "project_id":int(project_ids[-1]) if project_ids else None})
                return ProviderResult(text,len((system_prompt+context+user_prompt).split()),len(text.split()),"mock-local")
            engineering = any(x in user_prompt.lower() for x in ("build", "engineering", "software", "website"))
            tasks = ([{"title":"Define implementation scope","objective":user_prompt,"assignee_slug":"engineer","reviewer_slug":"engineering-director",
                "required_output":"Scoped implementation plan","acceptance_criteria":"Risks and deliverables are explicit"}] if engineering else
                [{"title":"Investigate market evidence","objective":user_prompt,"assignee_slug":"researcher","reviewer_slug":"research-director",
                "required_output":"Evidence-backed research brief","acceptance_criteria":"Claims cite sources and uncertainties"}])
            if action and project_ids:
                text=json.dumps({"mode":"PROJECT_ACTION","executive_response":"I prepared additional governed work for the existing project.",
                    "project_id":int(project_ids[-1]),"tasks":tasks})
            else:
                text=json.dumps({"mode":"NEW_PROJECT","executive_response":"I prepared a focused, reviewable plan for Founder approval.",
                    "project":{"name":_project_name(user_prompt),"objective":user_prompt,"priority":"HIGH"},"tasks":tasks})
        elif "TASK_EXECUTION" in system_prompt:
            text=json.dumps({"result_summary":f"Completed assigned work: {user_prompt}",
              "knowledge_proposals":[{"kind":"EVIDENCE","title":"Mock execution evidence","content":"The governed Task execution path completed; this is system evidence, not external market research.",
                "source_ref":"mock:task-execution","rationale":None,"basis_knowledge_ids":[]}]})
        else:
            text=f"EXECUTIVE RESULT\nRequest addressed: {user_prompt}\nRecommendation: proceed with the smallest test, record evidence, and review before commitment."
        return ProviderResult(text,len((system_prompt+context+user_prompt).split()),len(text.split()),response_id="mock-response",status="completed")
class OpenAIProvider:
    def complete(self, model_config, system_prompt, user_prompt, context, max_output_tokens, response_schema=None):
        from openai import OpenAI
        kwargs={"model":model_config.model_name,"instructions":system_prompt,"input":f"{context}\n\nUSER REQUEST:\n{user_prompt}","max_output_tokens":max_output_tokens}
        if response_schema:
            kwargs["text"]={"format":{"type":"json_schema","name":response_schema["name"],"strict":True,"schema":response_schema["schema"]}}
        r=OpenAI(api_key=os.environ["OPENAI_API_KEY"]).responses.create(**kwargs)
        refusal=None
        for item in getattr(r,"output",[]) or []:
            for content in getattr(item,"content",[]) or []:
                if getattr(content,"type",None)=="refusal": refusal=getattr(content,"refusal",None)
        incomplete=getattr(getattr(r,"incomplete_details",None),"reason",None)
        return ProviderResult(getattr(r,"output_text","") or "",r.usage.input_tokens,r.usage.output_tokens,
          response_id=r.id,request_id=getattr(r,"_request_id",None),status=getattr(r,"status",None) or "completed",
          refusal=refusal,incomplete_reason=incomplete)
def _project_name(request):
    words=request.strip().rstrip(".").split()
    if "project" in [x.lower() for x in words]:
        words=words[:[x.lower() for x in words].index("project")]
    while words and words[0].lower() in {"create","start","a","an","the","build"}: words.pop(0)
    return " ".join(words[:8]).title() or "Founder Initiative"
def get_provider(key):
    if key=="mock": return MockProvider()
    if key=="openai": return OpenAIProvider()
    raise ValueError(f"Unknown provider: {key}")
