from dataclasses import dataclass, field
from copy import deepcopy
import json
import os
from .provider_protocol import ProviderHTTPError, canonical_incomplete_reason
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
    stop_reason: str|None=None
    cache_creation_input_tokens: int=0
    cache_read_input_tokens: int=0
    # Provider-observed evidence/tool metadata is runtime truth, not model text.
    # Defaults keep all existing providers backward compatible.
    sources: list[dict] = field(default_factory=list)
    provider_usage: dict = field(default_factory=dict)
    used_tools: list[dict] = field(default_factory=list)

def anthropic_compatible_schema(schema):
    """Use the SDK's non-mutating transform to preserve constraints as model-visible descriptions."""
    from anthropic import transform_schema
    def normalize_nullable(value):
        if isinstance(value,list): return [normalize_nullable(item) for item in value]
        if not isinstance(value,dict): return value
        normalized={key:normalize_nullable(item) for key,item in value.items()}
        types=normalized.get("type")
        if isinstance(types,list):
            remainder={key:item for key,item in normalized.items() if key!="type"}
            return {"anyOf":[{"type":kind,**deepcopy(remainder)} for kind in types]}
        return normalized
    return transform_schema(normalize_nullable(schema))
class MockProvider:
    def complete(self, model_config, system_prompt, user_prompt, context, max_output_tokens, response_schema=None, tool_mode=None):
        if "CHAIR_ROUTER" in system_prompt:
            invited=__import__("re").findall(r"INVITED_SLUG: ([a-z0-9-]+)",context)
            after="Route after round" in user_prompt
            text=json.dumps({"continue_meeting":not after,"next_speakers":[] if after else invited[:2],
              "reason":"Recommendation is ready for synthesis." if after else "Select the smallest useful speaker set.",
              "founder_input_required":False,"founder_question":None})
        elif "MEETING_CONTRIBUTION_ROUND1" in system_prompt:
            text=json.dumps({"position":"Readiness remains unproven.","actions":["Run the smallest governed validation."],
              "risk":"Configured capability may be mistaken for proven operation.","evidence_ids":[],"confidence":0.55})
        elif "MEETING_CONTRIBUTION_RESPONSE" in system_prompt:
            text=json.dumps({"relation":"AGREE","core_point":"Readiness remains unproven without qualifying evidence.",
              "controls":["Require a governed validation before commitment."],
              "risk":"Evidence sufficiency remains the primary gap.","evidence_ids":[]})
        elif "MEETING_CONTRIBUTION_LATER" in system_prompt:
            text=json.dumps({"has_material_contribution":False,"type":"MATERIAL_REFINEMENT",
              "core_point":"No materially new information.","controls":[],
              "risk":"No additional risk identified.","evidence_ids":[],"target_message_ids":[]})
        elif "MULTI_AGENT_ORCHESTRATION_V1" in system_prompt:
            task_ids=[int(value) for value in __import__("re").findall(r"Task #(\d+)",context)]
            # Deterministic test/dev behavior: run approved branches independently.
            # Real models receive the same strict schema and may add true dependencies.
            text=json.dumps({
              "strategy":"PARALLEL_DAG" if len(task_ids)>1 else "SERIAL",
              "rationale":"Approved Tasks can start independently in the deterministic mock environment.",
              "max_parallelism":min(4,max(1,len(task_ids))),
              "tasks":[{
                "task_id":task_id,"depends_on_task_ids":[],"role":"WORKER",
                "reason":"No persisted upstream dependency is required by the deterministic mock plan."
              } for task_id in task_ids]
            })
        elif "TASK_REVIEW" in system_prompt:
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
        elif "CEO_OPERATION_DECISION" in system_prompt:
            blocked="BLOCKED" in context
            meeting_result="MEETING RESULTS" in context and "Position:" in context
            if blocked and not meeting_result:
                action="MEETING"
                meeting={"question":"What bounded remediation resolves the material Review conflict?","budget_twd":0.5}
            else:
                action="CONTINUE"
                meeting=None
            task_ids=__import__("re").findall(r"#(\d+) BLOCKED",context)
            text=json.dumps({"action":action,
              "reason":"A material Review conflict requires independent resolution." if action=="MEETING" else "Persisted evidence supports bounded remediation.",
              "goal_status":"BLOCKED" if blocked else "IN_PROGRESS","confidence":0.8,
              "next_task_id":int(task_ids[-1]) if task_ids else None,
              "meeting":meeting,"hiring_need":None,"founder_request":None,
              "task_plan":None})
        elif "HR_ASSESSMENT" in system_prompt:
            departments=__import__("re").findall(r"DEPARTMENT #(\d+): ([^\n]+)",context)
            employees=__import__("re").findall(r"EMPLOYEE #(\d+): ([^;]+);",context)
            models=__import__("re").findall(r"MODEL #(\d+): ([^;]+);",context)
            engineering=next(((int(i),n) for i,n in departments if "Engineering" in n),None)
            hr=next(((int(i),n) for i,n in departments if "Human" in n),None)
            target=engineering or hr or (int(departments[0][0]),departments[0][1])
            manager=next((int(i) for i,n in employees if "Engineering Director" in n),int(employees[0][0]))
            model_id=int(models[0][0])
            text=json.dumps({"recommendation":"HIRE",
              "existing_staff_alternative":"Existing staff can cover a temporary probe but not durable independent capacity.",
              "need_duration":"PERSISTENT","duplicate_capability":"No exact active duplicate was identified.",
              "candidate_template_id":None,"target_department_id":target[0],
              "target_position":"Specialist","manager_employee_id":manager,
              "recommended_model_config_id":model_id,"estimated_input_tokens":1000,
              "estimated_output_tokens":500,"expected_calls_per_mission":2,
              "missions_per_month":4,"max_mission_budget_twd":1.2,
              "expected_benefit":"Adds durable independent capability.",
              "redundancy_risk":"LOW","alternatives":["Use existing staff","Temporary support"],
              "success_criteria":["Three decision-useful assignments"],
              "probation_assignments":3,"instructions":"Perform the approved specialist role and challenge unsupported assumptions."})
        elif "GOAL_VERIFICATION" in system_prompt:
            packet=json.loads(context.split("GOAL VERIFICATION EVIDENCE\n",1)[-1])
            criteria=packet.get("completion_criteria") or []
            text=json.dumps({
              "overall_status":"SATISFIED",
              "criteria":[{
                "criterion":criterion,"status":"SATISFIED",
                "evidence":["Completed governed Task and Review records are present."],
                "reason":"The mocked evidence packet satisfies this criterion."
              } for criterion in criteria],
              "summary":"Persisted workflow evidence satisfies the approved objective and criteria.",
              "recommended_action":"Produce the final Founder report."
            })
        elif "CEO_STATUS_REPORT" in system_prompt:
            snapshot=json.loads(context)
            mission=snapshot.get("priority_mission")
            workers=snapshot.get("employees_working") or []
            blockers=snapshot.get("blocking_incidents") or []
            actions=snapshot.get("founder_actions_required") or []
            if mission:
                summary=(f"{mission['title']} is in {mission['stage']} with "
                  f"{len(workers)} Employees working, {len(blockers)} blocking incidents, "
                  f"and {len(actions)} Founder actions required.")
            else:
                summary=(f"No Mission is active; {len(workers)} Employees are working, "
                  f"with {len(blockers)} blocking incidents and {len(actions)} Founder actions required.")
            text=json.dumps({"executive_summary":summary})
        elif "CEO_FOUNDER_REQUEST" in system_prompt:
            status=any(x in user_prompt.lower() for x in ("how is","status","progress"))
            action=any(x in user_prompt.lower() for x in (
              "add task","next action","continue project","solve ","research ",
              "investigate ","analyze ","have the team","ask the team",
              "解決","研究","調查","分析","讓團隊","叫員工","找出答案"))
            advisory=(not action and any(x in user_prompt.lower() for x in (
              "what do you think","advise","advice","should i","recommend","?")))
            project_ids=__import__("re").findall(r"Project #(\d+)",context)
            operation_ids=__import__("re").findall(r"Operation #(\d+)",context)
            follow_up=any(x in user_prompt.lower() for x in (
              "continue the current operation","continue this operation",
              "resume the current operation","proceed with the current operation"))
            if follow_up and operation_ids:
                text=json.dumps({"mode":"OPERATION_FOLLOW_UP",
                  "executive_response":"I will continue the current approved Operation.",
                  "project":None,"project_id":int(project_ids[-1]) if project_ids else None,
                  "tasks":[],"operation":None})
                return ProviderResult(text,len((system_prompt+context+user_prompt).split()),len(text.split()),"mock-local")
            if status:
                text=json.dumps({"mode":"STATUS_QUERY","executive_response":"The requested project status is summarized from current operating state.",
                    "project":None,"project_id":int(project_ids[-1]) if project_ids else None,"tasks":[],"operation":None})
                return ProviderResult(text,len((system_prompt+context+user_prompt).split()),len(text.split()),"mock-local")
            if advisory:
                text=json.dumps({"mode":"ADVISORY","executive_response":"I recommend treating this as a bounded decision: clarify the evidence required, then authorize only the smallest useful next action.",
                    "project":None,"project_id":None,"tasks":[],"operation":None})
                return ProviderResult(text,len((system_prompt+context+user_prompt).split()),len(text.split()),"mock-local")
            engineering = any(x in user_prompt.lower() for x in ("build", "engineering", "software", "website"))
            tasks = ([{"title":"Define implementation scope","objective":user_prompt,"assignee_slug":"engineer","reviewer_slug":"engineer",
                "required_output":"Scoped implementation plan","acceptance_criteria":"Risks and deliverables are explicit"}] if engineering else
                [{"title":"Investigate market evidence","objective":user_prompt,"assignee_slug":"researcher","reviewer_slug":"research-director",
                "required_output":"Evidence-backed research brief","acceptance_criteria":"Claims cite sources and uncertainties"}])
            if action and project_ids:
                text=json.dumps({"mode":"PROJECT_ACTION","executive_response":"I prepared additional governed work for the existing project.",
                    "project":None,"project_id":int(project_ids[-1]),"tasks":tasks,"operation":None})
            elif "project" in user_prompt.lower():
                text=json.dumps({"mode":"NEW_PROJECT","executive_response":"I prepared a focused, reviewable plan for Founder approval.",
                    "project":{"name":_project_name(user_prompt),"objective":user_prompt,"priority":"HIGH"},
                    "project_id":None,"tasks":tasks,"operation":None})
            else:
                roster_ids={slug:int(value) for value,slug in __import__("re").findall(
                  r"ID: (\d+); slug: ([a-z0-9-]+)",context)}
                assignee=roster_ids.get("engineer" if engineering else "researcher")
                reviewer=roster_ids.get("engineer" if engineering else "research-director")
                text=json.dumps({"mode":"OPERATION_PLAN","executive_response":"I prepared one bounded internal operation for Founder approval.",
                    "project":None,"project_id":None,"tasks":[],"operation":{
                      "title":_project_name(user_prompt),"objective":user_prompt,"project_id":None,
                      "budget_twd":2.0,"tasks":[{"title":tasks[0]["title"],"objective":tasks[0]["objective"],
                        "assignee_employee_id":assignee,"reviewer_employee_id":reviewer,
                        "acceptance_criteria":[tasks[0]["acceptance_criteria"]]}],
                      "meeting_policy":"NEVER" if engineering else "AUTO",
                      "meeting_config":{
                        "trigger":"NEVER" if engineering else "BEFORE_FINAL_REPORT",
                        "participant_employee_ids":[] if engineering else [assignee,reviewer],
                        "max_rounds":1 if engineering else 2,"max_speakers_per_round":1 if engineering else 3,
                        "contribution_output_cap":256 if engineering else 512,"token_limit":6000 if engineering else 11000,
                        "budget_twd":0 if engineering else 0.8,"retry_limit":0 if engineering else 1},
                      "completion_criteria":["Assigned work is accepted","CEO report is persisted"]}})
        elif "TASK_EXECUTION" in system_prompt:
            text=json.dumps({"result_summary":f"Completed assigned work: {user_prompt}",
              "knowledge_proposals":[{"kind":"EVIDENCE","title":"Mock execution evidence","content":"The governed Task execution path completed; this is system evidence, not external market research.",
                "source_ref":"mock:task-execution","rationale":None,"basis_knowledge_ids":[]}]})
        else:
            text=f"EXECUTIVE RESULT\nRequest addressed: {user_prompt}\nRecommendation: proceed with the smallest test, record evidence, and review before commitment."
        return ProviderResult(text,len((system_prompt+context+user_prompt).split()),len(text.split()),response_id="mock-response",status="completed")
class OpenAIProvider:
    @staticmethod
    def _plain(value):
        if value is None:
            return None
        if isinstance(value,(str,int,float,bool)):
            return value
        if isinstance(value,dict):
            return {str(k):OpenAIProvider._plain(v) for k,v in value.items()}
        if isinstance(value,(list,tuple)):
            return [OpenAIProvider._plain(v) for v in value]
        if hasattr(value,"model_dump"):
            return OpenAIProvider._plain(value.model_dump())
        if hasattr(value,"dict"):
            return OpenAIProvider._plain(value.dict())
        if hasattr(value,"__dict__"):
            return OpenAIProvider._plain(vars(value))
        return str(value)

    @classmethod
    def _web_sources(cls,response):
        rows=[]
        seen=set()
        def add(url, *, title=None, snippet=None, source="web_search"):
            url=str(url or "").strip()
            if not url or url in seen:
                return
            seen.add(url)
            rows.append({
                "url":url,
                "title":str(title or "").strip() or None,
                "date":None,
                "last_updated":None,
                "snippet":str(snippet or "").strip() or None,
                "source":source,
            })
        for item in getattr(response,"output",[]) or []:
            if getattr(item,"type",None)=="web_search_call":
                action=getattr(item,"action",None)
                for source in getattr(action,"sources",None) or []:
                    data=cls._plain(source) or {}
                    if isinstance(data,dict):
                        add(data.get("url"), title=data.get("title"), snippet=data.get("snippet"), source="web_search_call")
            for content in getattr(item,"content",[]) or []:
                for annotation in getattr(content,"annotations",[]) or []:
                    data=cls._plain(annotation) or {}
                    if isinstance(data,dict) and (data.get("url") or data.get("type") in {"url_citation","citation"}):
                        add(data.get("url"), title=data.get("title"), source="response_citation")
        return rows

    def complete(self, model_config, system_prompt, user_prompt, context, max_output_tokens, response_schema=None, tool_mode=None):
        from openai import OpenAI
        kwargs={"model":model_config.model_name,"instructions":system_prompt,"input":f"{context}\n\nUSER REQUEST:\n{user_prompt}","max_output_tokens":max_output_tokens}
        web_search = tool_mode == "web_search"
        if web_search:
            search_country=(os.environ.get("EASON_ONE_WEB_SEARCH_COUNTRY") or "TW").strip().upper()
            search_timezone=(os.environ.get("EASON_ONE_WEB_SEARCH_TIMEZONE") or "Asia/Taipei").strip()
            user_location={"type":"approximate"}
            if search_country:
                user_location["country"]=search_country
            if search_timezone:
                user_location["timezone"]=search_timezone
            kwargs["tools"]=[{"type":"web_search","user_location":user_location}]
            kwargs["tool_choice"]="required"
            # One Work attempt pays for at most one hosted web-search run.
            # This keeps Founder-approved request-fee budgeting exact and bounded.
            kwargs["max_tool_calls"]=1
            kwargs["parallel_tool_calls"]=False
            kwargs["include"]=["web_search_call.action.sources"]
        if response_schema:
            kwargs["text"]={"format":{"type":"json_schema","name":response_schema["name"],"strict":True,"schema":response_schema["schema"]}}
        r=OpenAI(api_key=os.environ["OPENAI_API_KEY"],max_retries=0).responses.create(**kwargs)
        refusal=None
        for item in getattr(r,"output",[]) or []:
            for content in getattr(item,"content",[]) or []:
                if getattr(content,"type",None)=="refusal": refusal=getattr(content,"refusal",None)
        incomplete=getattr(getattr(r,"incomplete_details",None),"reason",None)
        sources=self._web_sources(r) if web_search else []
        usage=self._plain(getattr(r,"usage",None)) or {}
        return ProviderResult(
          getattr(r,"output_text","") or "",r.usage.input_tokens,r.usage.output_tokens,
          response_id=r.id,request_id=getattr(r,"_request_id",None),status=getattr(r,"status",None) or "completed",
          refusal=refusal,incomplete_reason=incomplete,stop_reason=incomplete,
          sources=sources,provider_usage=usage if isinstance(usage,dict) else {},
          used_tools=([{"type":"WEB_SEARCH","provider":"openai","source_count":len(sources),
                       "user_location":{"type":"approximate","country":search_country or None,"timezone":search_timezone or None}}] if web_search else []),
        )

class AnthropicProvider:
    def complete(self,model_config,system_prompt,user_prompt,context,max_output_tokens,response_schema=None,tool_mode=None):
        from anthropic import Anthropic
        kwargs={"model":model_config.model_name,"max_tokens":max_output_tokens,"system":system_prompt,
          "messages":[{"role":"user","content":f"{context}\n\nUSER REQUEST:\n{user_prompt}"}]}
        if response_schema:
            kwargs["output_config"]={"format":{"type":"json_schema",
              "schema":anthropic_compatible_schema(response_schema["schema"])}}
        message=Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"],max_retries=0).messages.create(**kwargs)
        text="\n".join(
          getattr(block,"text","") for block in (getattr(message,"content",None) or [])
          if getattr(block,"type",None)=="text" and getattr(block,"text",None) is not None)
        usage=message.usage
        stop_reason=getattr(message,"stop_reason",None)
        refusal=(text or "Request refused") if stop_reason=="refusal" else None
        if stop_reason in {"end_turn","stop_sequence"}: status,incomplete="completed",None
        elif stop_reason=="refusal": status,incomplete="completed",None
        else: status,incomplete="incomplete",canonical_incomplete_reason(stop_reason or "unknown")
        return ProviderResult(text,usage.input_tokens,usage.output_tokens,
          response_id=message.id,request_id=getattr(message,"_request_id",None),status=status,
          refusal=refusal,incomplete_reason=incomplete,stop_reason=stop_reason,
          cache_creation_input_tokens=getattr(usage,"cache_creation_input_tokens",0) or 0,
          cache_read_input_tokens=getattr(usage,"cache_read_input_tokens",0) or 0)

class GeminiProvider:
    def complete(self,model_config,system_prompt,user_prompt,context,max_output_tokens,response_schema=None,tool_mode=None):
        import urllib.request
        import urllib.error
        key=os.environ["GEMINI_API_KEY"]
        model=model_config.model_name
        endpoint=(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}")
        generation={"maxOutputTokens":max_output_tokens}
        if response_schema:
            generation["responseMimeType"]="application/json"
            generation["responseJsonSchema"]=response_schema["schema"]
        body={
          "systemInstruction":{"parts":[{"text":system_prompt}]},
          "contents":[{"role":"user","parts":[{"text":f"{context}\n\nUSER REQUEST:\n{user_prompt}"}]}],
          "generationConfig":generation,
        }
        request=urllib.request.Request(endpoint,data=json.dumps(body).encode("utf-8"),headers={"Content-Type":"application/json"},method="POST")
        try:
            with urllib.request.urlopen(request,timeout=180) as response:
                payload=json.loads(response.read().decode("utf-8"))
                request_id=response.headers.get("x-request-id")
        except urllib.error.HTTPError as exc:
            detail=exc.read().decode("utf-8",errors="replace")
            raise ProviderHTTPError(
                f"Gemini API error {exc.code}: {detail[:1200]}",
                status_code=exc.code, headers=getattr(exc, "headers", None),
            ) from exc
        candidates=payload.get("candidates") or []
        candidate=candidates[0] if candidates else {}
        parts=((candidate.get("content") or {}).get("parts") or [])
        text="\n".join(str(part.get("text") or "") for part in parts if part.get("text") is not None)
        usage=payload.get("usageMetadata") or {}
        finish=candidate.get("finishReason")
        complete=finish in {None,"STOP"}
        return ProviderResult(
          text=text,
          input_tokens=int(usage.get("promptTokenCount") or 0),
          output_tokens=int(usage.get("candidatesTokenCount") or usage.get("outputTokenCount") or 0),
          response_id=payload.get("responseId"),request_id=request_id,
          status="completed" if complete else "incomplete",
          incomplete_reason=None if complete else canonical_incomplete_reason(finish or "unknown"),
          stop_reason=finish,
        )

class PerplexityProvider:
    def complete(self,model_config,system_prompt,user_prompt,context,max_output_tokens,response_schema=None,tool_mode=None):
        from openai import OpenAI
        client=OpenAI(api_key=os.environ["PERPLEXITY_API_KEY"],base_url="https://api.perplexity.ai",max_retries=0)
        system=system_prompt
        kwargs={
          "model":model_config.model_name,
          "messages":[
            {"role":"system","content":system},
            {"role":"user","content":f"{context}\n\nUSER REQUEST:\n{user_prompt}"},
          ],
          "max_tokens":max_output_tokens,
        }
        if response_schema:
            # Perplexity Sonar accepts structured output through JSON Schema.
            # OpenAI's legacy {type: json_object} mode is not accepted by the
            # current Sonar API. Eason One still validates the exact schema
            # after the provider returns.
            kwargs["response_format"]={
              "type":"json_schema",
              "json_schema":{"schema":response_schema["schema"]},
            }
            kwargs["messages"][0]["content"] += (
              "\nReturn one JSON object matching this exact JSON Schema:\n"
              + json.dumps(response_schema["schema"],ensure_ascii=False)
            )
        result=client.chat.completions.create(**kwargs)
        choice=result.choices[0]
        text=choice.message.content or ""
        usage=getattr(result,"usage",None)
        finish=getattr(choice,"finish_reason",None)
        complete=finish in {None,"stop"}

        def plain(value):
            if value is None:
                return None
            if isinstance(value,(str,int,float,bool)):
                return value
            if isinstance(value,dict):
                return {str(k):plain(v) for k,v in value.items()}
            if isinstance(value,(list,tuple)):
                return [plain(v) for v in value]
            if hasattr(value,"model_dump"):
                return plain(value.model_dump())
            if hasattr(value,"dict"):
                return plain(value.dict())
            return str(value)

        citations=[str(url) for url in (getattr(result,"citations",None) or []) if str(url).strip()]
        search_rows=[]
        for row in (getattr(result,"search_results",None) or []):
            data=plain(row) or {}
            if not isinstance(data,dict):
                continue
            url=str(data.get("url") or "").strip()
            if not url:
                continue
            search_rows.append({
                "url":url,
                "title":str(data.get("title") or "").strip() or None,
                "date":data.get("date"),
                "last_updated":data.get("last_updated"),
                "snippet":str(data.get("snippet") or "").strip() or None,
                "source":str(data.get("source") or "web"),
            })
        known={row["url"] for row in search_rows}
        for url in citations:
            if url not in known:
                search_rows.append({"url":url,"title":None,"date":None,"last_updated":None,"snippet":None,"source":"citation"})
                known.add(url)
        usage_data=plain(usage) or {}
        return ProviderResult(
          text=text,
          input_tokens=int(getattr(usage,"prompt_tokens",0) or 0),
          output_tokens=int(getattr(usage,"completion_tokens",0) or 0),
          response_id=getattr(result,"id",None),
          request_id=getattr(result,"_request_id",None),
          status="completed" if complete else "incomplete",
          incomplete_reason=None if complete else canonical_incomplete_reason(finish or "unknown"),
          stop_reason=finish,
          sources=search_rows,
          provider_usage=usage_data if isinstance(usage_data,dict) else {},
          used_tools=[{"type":"WEB_SEARCH","provider":"perplexity","source_count":len(search_rows)}],
        )

def _project_name(request):
    words=request.strip().rstrip(".").split()
    if "project" in [x.lower() for x in words]:
        words=words[:[x.lower() for x in words].index("project")]
    while words and words[0].lower() in {"create","start","a","an","the","build"}: words.pop(0)
    return " ".join(words[:8]).title() or "Founder Initiative"
def get_provider(key):
    if key=="mock": return MockProvider()
    if key=="openai": return OpenAIProvider()
    if key=="anthropic": return AnthropicProvider()
    if key=="gemini": return GeminiProvider()
    if key=="perplexity": return PerplexityProvider()
    if key=="codex": raise ValueError("Codex is a local Engineering tool; use Codex Connector")
    raise ValueError(f"Unknown provider: {key}")
