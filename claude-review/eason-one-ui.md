# Eason One — UI Review Bundle


---

## FILE: eason_one\static\app.css

``css
:root{--bg:#061016;--panel:#0a1820;--line:#183746;--cyan:#48e7ff;--text:#d6edf2;--muted:#75939d;--green:#61f5a6}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 80% 0,#0c2a36,transparent 35%),var(--bg);color:var(--text);font:14px Inter,Segoe UI,sans-serif;display:flex;min-height:100vh}aside{width:220px;border-right:1px solid var(--line);padding:24px 16px;position:fixed;height:100vh;background:#071219cc}.brand{font-weight:800;letter-spacing:2px;color:var(--cyan);font-size:17px}.brand small{display:block;color:var(--muted);font-size:9px;margin:7px 0 35px}nav a{display:block;color:#9eb7bf;text-decoration:none;padding:12px;margin:4px 0;border-left:2px solid transparent}nav a:hover{color:var(--cyan);background:#0d2029;border-color:var(--cyan)}main{margin-left:220px;width:calc(100% - 220px);padding:32px;max-width:1600px}header{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:24px}h1{font-size:30px;margin:5px 0}h2{font-size:15px;text-transform:uppercase;letter-spacing:1px}h3,label{font-size:10px;letter-spacing:1.5px;color:var(--cyan)}p{color:#a4bcc3}.budget{text-align:right;font-size:24px;color:var(--green)}.budget small{display:block;color:var(--muted);font-size:9px}.metrics,.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:20px}.metrics>div,.panel,.employee{background:linear-gradient(135deg,#0d2029dd,#08151cdd);border:1px solid var(--line);padding:18px}.metrics b{display:block;font-size:18px;color:#eefcff}.metrics span,.stats small{display:block;color:var(--muted);font-size:9px;margin-top:6px}.grid{display:grid;grid-template-columns:1.4fr 1fr;gap:18px}.panel{margin-bottom:18px}.command textarea{min-height:76px}.row{display:flex;justify-content:space-between;gap:15px;padding:13px 0;border-bottom:1px solid #142b35;color:var(--text);text-decoration:none}.row span{min-width:0}.row small{display:block;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:550px;margin-top:4px}.row i,.task i{color:var(--cyan);font-size:10px;font-style:normal}.task{border:1px solid var(--line);padding:15px;margin:12px 0}.task>div{display:flex;justify-content:space-between}.task small{display:block;color:var(--muted)}input,textarea,select{width:100%;background:#061117;color:var(--text);border:1px solid #245064;padding:11px;margin:5px 0;font:inherit}textarea{min-height:70px}button,.button{background:var(--cyan);color:#03202a;border:0;padding:10px 15px;font-weight:700;cursor:pointer;text-decoration:none;display:inline-block;margin:5px 3px 5px 0}.secondary{background:#18323e;color:var(--text)}pre{white-space:pre-wrap;word-break:break-word;color:#a9cbd3;font:12px Consolas,monospace}.cards{grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}.employee{color:var(--text);text-decoration:none;transition:.2s}.employee:hover{border-color:var(--cyan);transform:translateY(-2px)}.employee h2{text-transform:none;font-size:21px;margin:8px 0}.orb{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 12px var(--green);float:right}.stats{display:grid;grid-template-columns:1fr 1fr;gap:10px;border-top:1px solid var(--line);padding-top:13px}.employee footer{color:var(--muted);font-size:10px;margin-top:15px}.chat>div{max-width:80%;padding:12px;margin:10px 0;border-left:2px solid var(--cyan);background:#07141a}.chat .employee{margin-left:auto;border:0;border-right:2px solid var(--green)}.muted{color:var(--muted)}.flash{padding:12px;border:1px solid var(--line);margin-bottom:15px}.flash.ok{border-color:#276f50}.flash.error{border-color:#8d3847}@media(max-width:800px){aside{position:static;width:100%;height:auto}body{display:block}main{margin:0;width:100%;padding:18px}.grid{grid-template-columns:1fr}nav a{display:inline-block}.brand small{margin-bottom:10px}}

``

---

## FILE: eason_one\templates\base.html

``html
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>{% block title %}Eason One{% endblock %}</title><link rel="stylesheet" href="{{url_for('static',filename='app.css')}}"></head><body><aside><div class="brand">◈ EASON ONE<small>FOUNDER COMMAND OS</small></div><nav><a href="/ceo">◆ {{t('nav.ceo')}}</a><a href="/projects">▦ {{t('nav.projects')}}</a><a href="/employees">◈ {{t('nav.employees')}}</a><a href="/models">◉ {{t('nav.models')}}</a><a href="/inbox">◇ {{t('nav.inbox')}}</a><a href="/costs">◎ {{t('nav.costs')}}</a></nav><div class="languages"><form method="post" action="/language/en"><input type="hidden" name="next" value="{{request.path}}"><button>{{t('lang.en')}}</button></form><span>|</span><form method="post" action="/language/zh-TW"><input type="hidden" name="next" value="{{request.path}}"><button>{{t('lang.zh')}}</button></form></div></aside><main>{% with ms=get_flashed_messages(with_categories=true) %}{% for c,m in ms %}<div class="flash {{c}}">{{m}}</div>{% endfor %}{% endwith %}{% block content %}{% endblock %}</main></body></html>

``

---

## FILE: eason_one\templates\ceo.html

``html
{% extends "base.html" %}{% block content %}<header><div><label>CEO OPERATING INTERFACE</label><h1>Company Command</h1></div><div class="budget">NT$ {{'%.2f'|format(remaining)}} <small>REMAINING / {{'%.0f'|format(company.real_budget_limit)}}</small></div></header>
<section class="metrics"><div><b>{{metrics.active_projects}}</b><span>Active Projects</span></div><div><b>{{metrics.active_tasks}}</b><span>Active Tasks</span></div><div><b>{{metrics.blocked}}</b><span>Blocked</span></div><div><b>{{metrics.pending}}</b><span>Founder Attention</span></div><div><b>NT$ {{'%.2f'|format(spent)}}</b><span>Real Spend</span></div></section>
<section class="panel command"><label>FOUNDER → CEO</label><form method="post"><textarea name="request" required placeholder="Create a Beauty LINE Consultation project and investigate whether this idea is worth pursuing."></textarea><button>{{t('button.execute')}}</button></form></section>{% if latest_ceo and latest_ceo.parsed_output_json %}<section class="panel"><label>CEO / {{latest_ceo.parsed_output_json.get('mode')}}</label><h2>{{latest_ceo.parsed_output_json.get('executive_response')}}</h2><a class="button" href="/runs/{{latest_ceo.id}}">Audit / Run Details</a></section>{% endif %}
<div class="grid"><section class="panel"><h2>Project Radar</h2>{% for p in projects %}<a class="row" href="/projects/{{p.id}}"><span><b>{{p.name}}</b><small>{{p.status}} · {{p.tasks|length}} tasks</small></span><i>{{p.priority}}</i></a>{% else %}<p class="muted">No projects. Issue a Founder request above.</p>{% endfor %}</section>
<section class="panel"><h2>Recently Completed</h2>{% for t in recent %}<div class="row"><span><b>{{t.title}}</b><small>{{t.project.name}}</small></span><i>DONE</i></div>{% else %}<p class="muted">No completed tasks yet.</p>{% endfor %}</section></div>{% endblock %}

``

---

## FILE: eason_one\templates\costs.html

``html
{% extends "base.html" %}{% block content %}<header><div><label>REAL MONEY / AUDIT LEDGER</label><h1>Cost Control</h1></div><div class="budget">NT$ {{'%.2f'|format(remaining)}}<small>REMAINING</small></div></header><section class="metrics"><div><b>NT$ {{'%.2f'|format(company.real_budget_limit)}}</b><span>Hard Budget</span></div><div><b>NT$ {{'%.6f'|format(spent)}}</b><span>Spent</span></div><div><b>NT$ {{'%.2f'|format(remaining)}}</b><span>Remaining</span></div></section><section class="panel"><h2>Auditable Ledger</h2>{% for e in events %}<a class="row" href="/runs/{{e.agent_run_id}}"><span><b>{{e.category}} · {{e.description}}</b><small>Employee {{e.employee_id}} · Project {{e.project_id or '-'}} · Task {{e.task_id or '-'}}</small></span><i>NT$ {{e.real_cost_delta}}</i></a>{% else %}<p class="muted">No cost events yet.</p>{% endfor %}</section>{% endblock %}

``

---

## FILE: eason_one\templates\employee.html

``html
{% extends "base.html" %}{% block content %}<header><div><label>{{e.department.name if e.department else 'CEO OFFICE / ASSURANCE'}}</label><h1>{{e.name}}</h1><p>{{e.role_description}}</p></div><a class="button" href="/employees/{{e.id}}/interview">{{t('button.interview')}}</a></header><section class="metrics"><div><b>{{e.position.name}} / L{{e.position.level}}</b><span>Position</span></div><div><b>{{e.manager.name if e.manager else 'Founder'}}</b><span>Manager</span></div><div><b>{{e.salary_credits_per_week}}</b><span>Weekly Internal Credits</span></div><div><b>{{cc}}</b><span>Company Contribution</span></div></section><div class="grid"><section class="panel"><h2>Work History</h2>{% for task in tasks %}<a class="row" href="/projects/{{task.project_id}}"><span><b>{{task.title}}</b><small>{{task.project.name}} · {{task.result_summary or 'No result yet'}}</small></span><i>{{task.status}}</i></a>{% else %}<p class="muted">No tasks.</p>{% endfor %}<h2>Learning / Experience</h2>{% for item in learning %}<div class="row"><span><b>{{item.title}}</b><small>{{item.content}}</small></span><i>{{'VALIDATED' if item.validated else 'UNVALIDATED'}}</i></div>{% else %}<p class="muted">No learning records.</p>{% endfor %}</section><section><div class="panel"><h2>Model Assignment</h2><b>{{e.current_model.label if e.current_model else 'None'}}</b><p>{{e.current_model.provider_key if e.current_model else ''}} / {{e.current_model.model_name if e.current_model else ''}}</p><form method="post" action="/employees/{{e.id}}/model"><select name="model_config_id">{% for model in model_configs %}<option value="{{model.id}}" {{'selected' if e.current_model_config_id==model.id}}>{{model.label}} · {{model.model_name}}</option>{% endfor %}</select><input name="reason" placeholder="Reason for reassignment"><button>Change Model</button></form><h3>Model History</h3>{% for h in history %}<p>{{h.model_config.label if h.model_config else 'None'}} · {{h.reason or ''}}</p>{% endfor %}</div><div class="panel"><h2>Project Contribution</h2>{% for project,value in project_contributions %}<div class="row"><b>{{project.name}}</b><i>{{value}}</i></div>{% else %}<p class="muted">No Project Contribution.</p>{% endfor %}</div><div class="panel"><h2>Add Learning Record</h2><form method="post" action="/employees/{{e.id}}/learning"><input name="title" required placeholder="Title"><textarea name="content" required placeholder="Evidence-based learning"></textarea><input name="source_ref" placeholder="Source reference"><select name="project_id"><option value="">Company-level</option>{% for project in projects %}<option value="{{project.id}}">{{project.name}}</option>{% endfor %}</select><label><input type="checkbox" name="validated" value="1"> Founder validated</label><button>Add Learning</button></form></div><div class="panel"><h2>Execution Audit</h2>{% for run in runs %}<a class="row" href="/runs/{{run.id}}"><span><b>{{run.purpose}}</b><small>{{run.started_at}}</small></span><i>{{run.status}}</i></a>{% else %}<p class="muted">No runs.</p>{% endfor %}</div></section></div>{% endblock %}

``

---

## FILE: eason_one\templates\employees.html

``html
{% extends "base.html" %}{% block content %}<header><div><label>PERSISTENT ORGANIZATION</label><h1>Employees</h1></div></header><section class="cards">{% for r in rows %}<a class="employee" href="/employees/{{r.e.id}}"><div class="orb"></div><label>{{r.e.department.name if r.e.department else 'CEO OFFICE / ASSURANCE'}}</label><h2>{{r.e.name}}</h2><p>{{r.e.position.name}} · L{{r.e.position.level}}</p><div class="stats"><span>{{r.active_tasks|length}}<small>ACTIVE TASKS</small></span><span>{{r.project_contribution}}<small>PROJECT CONTRIB.</small></span><span>{{r.company_contribution}}<small>COMPANY CONTRIB.</small></span><span>NT${{'%.3f'|format(r.cost)}}<small>REAL COST</small></span></div><footer>{{r.e.current_model.label if r.e.current_model else 'NO MODEL'}} · {{r.tokens}} TOKENS</footer></a>{% endfor %}</section>{% endblock %}

``

---

## FILE: eason_one\templates\inbox.html

``html
{% extends "base.html" %}{% block content %}<header><div><label>GOVERNANCE</label><h1>Founder Inbox</h1></div></header><section class="panel">{% for p in proposals %}<div class="task"><div><b>Proposal #{{p.id}} · {{p.payload_json.get('type','KNOWLEDGE')}}</b><small>{{p.status}} · Employee {{p.proposed_by_employee_id}}</small></div><pre>{{p.payload_json|tojson(indent=2)}}</pre>{% if p.status=='PENDING' %}{% if p.payload_json.get('type')=='PROJECT_PLAN' %}<form method="post" action="/inbox/{{p.id}}/materialize"><button>Confirm Governed Plan</button></form><form method="post" action="/inbox/{{p.id}}/reject"><button class="secondary">Reject</button></form>{% else %}<form method="post" action="/inbox/{{p.id}}/knowledge-review"><button name="decision" value="APPROVED">Approve Knowledge</button><button name="decision" value="REJECTED" class="secondary">Reject</button></form><details><summary>Correct before approval</summary><form method="post" action="/inbox/{{p.id}}/knowledge-review"><input type="hidden" name="decision" value="CORRECTED"><select name="kind"><option>FACT</option><option>HYPOTHESIS</option><option>EVIDENCE</option><option>DECISION</option><option>CORRECTION</option><option>KILLED</option></select><input name="title" required value="{{p.payload_json.get('title','')}}"><textarea name="content" required>{{p.payload_json.get('content','')}}</textarea><input name="source_ref" value="{{p.payload_json.get('source_ref','') or ''}}" placeholder="Evidence source"><input name="rationale" value="{{p.payload_json.get('rationale','') or ''}}" placeholder="Decision rationale"><input name="target_knowledge_id" value="{{p.payload_json.get('target_knowledge_id','') or ''}}" placeholder="Target knowledge ID"><input name="note" placeholder="Founder correction note"><button>Materialize Corrected Knowledge</button></form></details>{% endif %}{% endif %}</div>{% else %}<p class="muted">No proposals.</p>{% endfor %}</section>{% endblock %}

``

---

## FILE: eason_one\templates\interview.html

``html
{% extends "base.html" %}{% block content %}<header><div><label>FOUNDER INSPECTION CHANNEL</label><h1>Interview · {{e.name}}</h1><p>Conversation persists but cannot mutate policy or authoritative state.</p></div></header><section class="panel chat">{% for m in interview.messages %}<div class="{{m.speaker|lower}}"><label>{{m.speaker}}</label><p>{{m.content}}</p></div>{% else %}<p class="muted">Ask about current work, decisions, blockers, or lessons.</p>{% endfor %}<form method="post"><textarea name="content" required placeholder="Ask {{e.name}}…"></textarea><button>Send & Execute</button></form></section>{% endblock %}

``

---

## FILE: eason_one\templates\models.html

``html
{% extends "base.html" %}{% block content %}<header><div><label>FOUNDER CONFIGURATION</label><h1>Model Management</h1><p>API secrets remain in environment variables. Company remaining budget: TWD {{remaining}}.</p></div></header><div class="grid"><section class="panel"><h2>Configured Models</h2>{% for m in models %}<div class="task"><div><b>{{m.label}}</b><i>{{'ACTIVE' if m.active else 'INACTIVE'}}</i></div><p>{{m.provider_key}} / {{m.model_name}} · {{m.currency}} · input {{m.input_price_per_million}} / output {{m.output_price_per_million}} per million · max {{m.max_output_tokens}}</p><p>Conservative representative maximum: {{m.currency}} {{estimates[m.id]}}</p><form method="post" action="/models/{{m.id}}/toggle"><button class="secondary">{{'Deactivate' if m.active else 'Activate'}}</button></form></div>{% endfor %}</section><section class="panel"><h2>Create ModelConfig</h2><form method="post"><input name="label" required placeholder="Label"><select name="provider_key"><option>mock</option><option>openai</option></select><input name="model_name" required placeholder="Exact model name"><input name="input_price_per_million" required value="0"><input name="output_price_per_million" required value="0"><input name="currency" required value="TWD"><input name="max_output_tokens" type="number" min="1" required value="1200"><button>Create Model Configuration</button></form></section></div>{% endblock %}

``

---

## FILE: eason_one\templates\project.html

``html
{% extends "base.html" %}{% block content %}<header><div><label>PROJECT / {{p.status}}</label><h1>{{p.name}}</h1><p>{{p.objective}}</p></div><div class="budget">NT$ {{'%.4f'|format(costs)}}<small>PROJECT COST</small></div></header><section class="metrics">{% for s in ['TODO','ASSIGNED','WORKING','BLOCKED','REVIEW','DONE'] %}<div><b>{{counts.get(s,0)}}</b><span>{{s}}</span></div>{% endfor %}</section><div class="grid"><section class="panel"><h2>Task Board</h2>{% for task in p.tasks %}<div class="task"><div><b>{{task.title}}</b><small>{{task.assigned_employee.name if task.assigned_employee else 'Unassigned'}} · reviewer {{task.reviewer.name if task.reviewer else 'CEO'}}</small></div><i>{{task.status}}</i><p>{{task.objective}}</p>{% if task.result_summary %}<details><summary>Result</summary><pre>{{task.result_summary}}</pre></details>{% endif %}{% if task.status in ['ASSIGNED','WORKING'] %}<form method="post" action="/tasks/{{task.id}}/run"><button>Run Structured Task</button></form>{% endif %}{% if task.status=='REVIEW' %}<form method="post" action="/tasks/{{task.id}}/run-review"><input name="instruction" value="Review this Task"><button>Run Reviewer · {{task.reviewer.name if task.reviewer else 'CEO'}}</button></form>{% endif %}</div>{% endfor %}<h2>Current Effective Knowledge</h2>{% for k in knowledge %}<div class="row"><span><b>{{k.kind}} #{{k.id}} · {{k.title}}</b><small>{{k.content}}{% if decision_bases.get(k.id) %}<br>Based on: {% for basis in decision_bases[k.id] %}{{basis.kind}} #{{basis.id}} {{basis.title}}{{', ' if not loop.last}}{% endfor %}{% endif %}</small></span></div>{% else %}<p class="muted">No effective knowledge.</p>{% endfor %}<details><summary>Historical Knowledge</summary>{% for k in knowledge_history %}<div class="row"><span><b>{{k.kind}} #{{k.id}} · {{k.title}}</b><small>{{k.content}}</small></span></div>{% endfor %}</details></section><section><div class="panel"><h2>CEO Briefing</h2><form method="post" action="/projects/{{p.id}}/briefing"><button>Generate CEO Briefing</button></form>{% for report in reports %}<a class="row" href="/runs/{{report.agent_run_id}}"><span><b>{{report.content}}</b></span><i>REPORT</i></a>{% endfor %}</div><div class="panel"><h2>Founder Knowledge Capture</h2><form method="post" action="/projects/{{p.id}}/knowledge"><select name="kind"><option>FACT</option><option>HYPOTHESIS</option><option>EVIDENCE</option><option>DECISION</option><option>CORRECTION</option><option>KILLED</option></select><input name="title" required placeholder="Title"><textarea name="content" required placeholder="Knowledge content"></textarea><input name="source_ref" placeholder="Evidence source"><input name="rationale" placeholder="Decision rationale"><select name="target_knowledge_id"><option value="">No correction/kill target</option>{% for k in knowledge_targets %}<option value="{{k.id}}">#{{k.id}} {{k.kind}} · {{k.title}}</option>{% endfor %}</select><label>Decision basis (Ctrl/Cmd for multiple)</label><select name="basis_knowledge_ids" multiple>{% for k in basis_options %}<option value="{{k.id}}">#{{k.id}} {{k.kind}} · {{k.title}}</option>{% endfor %}</select><button>Record Founder Knowledge</button></form></div><div class="panel"><h2>Create Task</h2><form method="post" action="/projects/{{p.id}}/tasks"><input name="title" required><textarea name="objective" required></textarea><select name="assignee_id">{% for e in employees %}<option value="{{e.id}}">{{e.name}}</option>{% endfor %}</select><select name="reviewer_id"><option value="">CEO fallback</option>{% for e in employees %}<option value="{{e.id}}">{{e.name}}</option>{% endfor %}</select><input name="required_output"><input name="acceptance_criteria"><button>Create & Assign</button></form></div></section></div>{% endblock %}

``

---

## FILE: eason_one\templates\projects.html

``html
{% extends "base.html" %}{% block content %}<header><div><label>PORTFOLIO</label><h1>Projects</h1></div></header><section class="panel">{% for p in projects %}<a class="row" href="/projects/{{p.id}}"><span><b>{{p.name}}</b><small>{{p.objective}}</small></span><i>{{p.status}}</i></a>{% else %}<p class="muted">No governed projects yet. Use the CEO interface.</p>{% endfor %}</section>{% endblock %}

``

---

## FILE: eason_one\templates\run.html

``html
{% extends "base.html" %}{% block content %}<header><div><label>AGENT RUN #{{run.id}} / {{run.status}}</label><h1>{{run.purpose}}</h1></div><div class="budget">{{run.currency_snapshot}} {{run.real_cost or 0}}<small>{{run.input_tokens or 0}} IN / {{run.output_tokens or 0}} OUT</small></div></header><div class="grid"><section class="panel"><h2>Output</h2><pre>{{run.raw_output or run.error_text or 'No output'}}</pre>{% if run.parsed_output_json %}<h2>Validated Structured Output</h2><pre>{{run.parsed_output_json|tojson(indent=2)}}</pre>{% endif %}<h2>User Request</h2><pre>{{run.user_request}}</pre></section><section class="panel"><h2>Historical Execution Identity</h2><p>Employee: {{run.employee.name}}</p><p>Provider: {{run.provider_key_snapshot}}</p><p>Model: {{run.model_name_snapshot}}</p><p>Pricing: {{run.currency_snapshot}} {{run.input_price_snapshot}} input / {{run.output_price_snapshot}} output per million</p><p>Provider request: {{run.provider_request_id or '-'}}</p><h3>Current configuration (not historical)</h3><p>{{run.model_config.label}} / {{run.model_config.model_name}}</p><h2>Prompt Snapshot</h2><pre>{{run.system_prompt_snapshot}}</pre><h2>Context Snapshot</h2><pre>{{run.context_snapshot}}</pre></section></div>{% endblock %}

``

---

## FILE: eason_one\templates\unseeded.html

``html
{% extends "base.html" %}{% block content %}<h1>Company not initialized</h1><div class="panel"><p>Run <code>flask --app run.py seed</code>, then reload.</p></div>{% endblock %}

``
