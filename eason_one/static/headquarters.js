(() => {
  const qs = (selector, root = document) => root.querySelector(selector);
  const qsa = (selector, root = document) => [...root.querySelectorAll(selector)];

  const clock = qs('[data-hq-clock]');
  if (clock) {
    const tick = () => {
      clock.textContent = new Intl.DateTimeFormat('en-GB', {
        hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
      }).format(new Date());
    };
    tick();
    window.setInterval(tick, 1000);
  }


  const companyPulse = qs('[data-company-pulse]');
  if (companyPulse) {
    let pulseRefreshBusy = false;
    let pulseTimer = null;

    const stampPulseSync = () => {
      const stamp = qs('[data-pulse-synced]', companyPulse);
      if (stamp) stamp.textContent = new Intl.DateTimeFormat('en-GB', {
        hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
      }).format(new Date());
    };

    const refreshCompanyPulse = async () => {
      if (pulseRefreshBusy || document.hidden) return;
      pulseRefreshBusy = true;
      try {
        const response = await fetch('/headquarters/pulse', {
          method: 'GET',
          headers: {'Accept': 'text/html'},
          cache: 'no-store',
        });
        if (!response.ok) return;
        const html = await response.text();
        if (!html.trim()) return;
        companyPulse.innerHTML = html;
        stampPulseSync();
      } catch (_error) {
        // Pulse is an observability projection. A failed refresh must never
        // mutate runtime state or manufacture an alarming fake failure card.
      } finally {
        pulseRefreshBusy = false;
      }
    };

    stampPulseSync();
    pulseTimer = window.setInterval(refreshCompanyPulse, 2500);
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) refreshCompanyPulse();
    });
    window.addEventListener('beforeunload', () => {
      if (pulseTimer) window.clearInterval(pulseTimer);
    }, {once: true});
  }

  const dock = qs('[data-command-dock]');
  const setDock = (open) => {
    if (!dock) return;
    dock.classList.toggle('is-open', open);
    dock.setAttribute('aria-hidden', open ? 'false' : 'true');
    document.body.style.overflow = open ? 'hidden' : '';
    if (open) window.setTimeout(() => qs('textarea', dock)?.focus(), 120);
  };
  qsa('[data-command-open]').forEach((button) => button.addEventListener('click', () => setDock(true)));
  qsa('[data-command-close]').forEach((button) => button.addEventListener('click', () => setDock(false)));

  qsa('[data-dismiss]').forEach((button) => {
    button.addEventListener('click', () => button.closest('[data-dismissable]')?.remove());
  });

  qsa('[data-task-open]').forEach((button) => {
    button.addEventListener('click', () => {
      const drawer = document.getElementById(button.dataset.taskOpen);
      if (!drawer) return;
      drawer.classList.add('is-open');
      drawer.setAttribute('aria-hidden', 'false');
      document.body.style.overflow = 'hidden';
    });
  });
  qsa('[data-drawer-close]').forEach((button) => {
    button.addEventListener('click', () => {
      const drawer = button.closest('[data-context-drawer]');
      drawer?.classList.remove('is-open');
      drawer?.setAttribute('aria-hidden', 'true');
      document.body.style.overflow = '';
    });
  });

  const tabs = qs('[data-tabs]');
  if (tabs) {
    const buttons = qsa('[data-tab-target]', tabs);
    const panels = qsa('[data-tab-panel]');
    const activate = (name, updateHash = true) => {
      buttons.forEach((button) => button.classList.toggle('is-active', button.dataset.tabTarget === name));
      panels.forEach((panel) => panel.classList.toggle('is-active', panel.dataset.tabPanel === name));
      if (updateHash) history.replaceState(null, '', `#${name}`);
    };
    buttons.forEach((button) => button.addEventListener('click', () => activate(button.dataset.tabTarget)));
    const initial = location.hash.slice(1);
    if (initial && buttons.some((button) => button.dataset.tabTarget === initial)) activate(initial, false);
  }

  const modifyPanel = qs('[data-modify-panel]');
  qsa('[data-open-modify]').forEach((button) => button.addEventListener('click', () => {
    if (!modifyPanel) return;
    modifyPanel.hidden = false;
    document.body.style.overflow = 'hidden';
  }));
  qsa('[data-close-modify]').forEach((button) => button.addEventListener('click', () => {
    if (!modifyPanel) return;
    modifyPanel.hidden = true;
    document.body.style.overflow = '';
  }));

  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    if (dock?.classList.contains('is-open')) setDock(false);
    qsa('[data-context-drawer].is-open').forEach((drawer) => {
      drawer.classList.remove('is-open');
      drawer.setAttribute('aria-hidden', 'true');
    });
    if (modifyPanel && !modifyPanel.hidden) modifyPanel.hidden = true;
    document.body.style.overflow = '';
  });


  const officeForm = qs('[data-ceo-office-form]');
  if (officeForm) {
    const thread = qs('[data-ceo-thread]');
    const runtime = qs('[data-ceo-runtime]');
    const routeLabel = qs('[data-ceo-route]', runtime);
    const elapsedLabel = qs('[data-ceo-elapsed]', runtime);
    const detailLabel = qs('[data-runtime-detail]', runtime);
    const submitButton = qs('button[type="submit"]', officeForm);
    const textarea = qs('[name="request"]', officeForm);
    const dialogueState = qs('[data-dialogue-state]');
    const projectId = officeForm.dataset.projectId || null;
    const livePanel = qs('[data-ceo-operation-live]');
    const steps = Object.fromEntries(
      qsa('[data-runtime-step]', runtime).map((node) => [node.dataset.runtimeStep, node])
    );
    let timer = null;
    let activeRunner = null;
    let finalReportShownFor = null;

    const setDialogueState = (value) => {
      if (dialogueState) dialogueState.textContent = value;
    };

    const setStep = (name, state) => {
      const node = steps[name];
      if (!node) return;
      node.className = state ? `is-${state}` : '';
    };

    const element = (tag, className, text) => {
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (text !== undefined && text !== null) node.textContent = text;
      return node;
    };

    const scrollThread = () => {
      if (thread) thread.scrollTop = thread.scrollHeight;
    };

    const appendMessage = (role, text, options = {}) => {
      if (!thread) return null;
      qs('[data-dialogue-empty]', thread)?.remove();
      const article = element('article', `dialogue-message role-${role.toLowerCase()}`);
      article.append(element('span', '', role));
      const body = element('div');
      String(text || '').split(/\n{2,}/).filter(Boolean).forEach((paragraph) => {
        body.append(element('p', '', paragraph.trim()));
      });
      if (!body.children.length) body.textContent = text || '';
      article.append(body);
      if (options.runId) {
        const audit = element('a', '', `Run #${options.runId}`);
        audit.href = `/headquarters/system/runs/${options.runId}`;
        article.append(audit);
      }
      thread.append(article);
      scrollThread();
      return article;
    };

    let thinkingMessage = null;
    const showThinking = (text = 'Thinking through the company state…') => {
      if (!thread) return;
      if (!thinkingMessage) {
        qs('[data-dialogue-empty]', thread)?.remove();
        thinkingMessage = element('article', 'dialogue-message role-ceo is-thinking');
        thinkingMessage.dataset.ceoThinkingBubble = 'true';
        thinkingMessage.append(element('span', '', 'CEO'));
        const body = element('div', 'ceo-thinking-body');
        body.append(element('p', 'ceo-thinking-copy', text));
        const dots = element('div', 'ceo-thinking-dots');
        dots.setAttribute('aria-label', 'CEO is thinking');
        dots.append(element('i'), element('i'), element('i'));
        body.append(dots);
        thinkingMessage.append(body);
        thread.append(thinkingMessage);
      } else {
        const copy = qs('.ceo-thinking-copy', thinkingMessage);
        if (copy) copy.textContent = text;
      }
      scrollThread();
    };
    const hideThinking = () => {
      thinkingMessage?.remove();
      thinkingMessage = null;
    };

    const proposalCard = (proposal) => {
      const card = element('section', 'ceo-proposal-card proposal-founder-v2');
      card.dataset.operationId = proposal.operation_id;
      card.dataset.createsProject = proposal.creates_project ? '1' : '0';

      const head = element('header', 'proposal-founder-head');
      const heading = element('div');
      heading.append(element('span', 'hq-kicker', 'CEO RECOMMENDATION'));
      heading.append(element('h3', '', proposal.creates_project ? (proposal.project_name || proposal.title) : proposal.title));
      head.append(heading, element('b', 'proposal-founder-verdict', 'YES · RUN THIS'));
      card.append(head);

      card.append(element(
        'p', 'proposal-objective',
        proposal.creates_project ? (proposal.project_objective || proposal.objective) : proposal.objective
      ));

      const writePaths = (proposal.tasks || []).flatMap((task) => task.write_paths || []);
      const deliverable = (() => {
        if (writePaths.some((path) => String(path).toLowerCase().endsWith('.html'))) {
          return 'A decision-ready HTML brief you can open in your browser';
        }
        if (writePaths.some((path) => String(path).toLowerCase().endsWith('.pdf'))) {
          return 'A finished PDF deliverable you can read or share';
        }
        if (writePaths.some((path) => /\.(md|txt)$/i.test(String(path)))) {
          return 'A finished written brief with traceable evidence';
        }
        return proposal.creates_project
          ? `A completed result for ${proposal.project_name || proposal.title}`
          : `A completed result for ${proposal.title}`;
      })();
      const planTitles = (proposal.tasks || []).map((task) => task.title).filter(Boolean);
      const summary = element('div', 'proposal-founder-summary');
      const get = element('article');
      get.append(element('span', '', 'YOU WILL GET'), element('b', '', deliverable));
      const cost = element('article');
      cost.append(
        element('span', '', 'COST'),
        element('b', '', `Est. NT$ ${proposal.budget_twd} · cap NT$ ${proposal.project_budget_twd || proposal.budget_twd}`)
      );
      const plan = element('article');
      plan.append(element('span', '', 'COMPANY WILL'), element('b', '', planTitles.join(' → ') || 'Execute the bounded company plan'));
      const action = element('article');
      action.append(
        element('span', '', 'YOU DECIDE NOW'),
        element('b', '', 'Run it, or decline it. Technical details are optional.')
      );
      summary.append(get, cost, plan, action);
      card.append(summary);

      const actions = element('footer', 'proposal-actions proposal-founder-actions');
      const approve = element('button', 'hq-button proposal-yes', 'YES — RUN IT');
      approve.type = 'button';
      approve.dataset.approveOperation = proposal.operation_id;
      approve.dataset.createsProject = proposal.creates_project ? '1' : '0';
      const decline = element('button', 'hq-button secondary proposal-no', 'NO — DECLINE');
      decline.type = 'button';
      decline.dataset.declineOperation = proposal.operation_id;
      actions.append(approve, decline);
      card.append(actions);

      const details = element('details', 'proposal-company-plan');
      const summaryLabel = element('summary', '', 'View company plan / authority / technical details');
      const detailBody = element('div', 'proposal-company-plan-body');
      if (proposal.creates_project) {
        const projectContract = element('div', 'proposal-project-contract');
        projectContract.append(element('span', 'hq-kicker', 'FOUNDER PROJECT AUTHORITY'));
        projectContract.append(element('b', '', proposal.project_budget_twd ? `Project budget NT$ ${proposal.project_budget_twd}` : 'Project authority uses the approved envelope'));
        if ((proposal.project_success_criteria || []).length) projectContract.append(element('p', '', `Success: ${proposal.project_success_criteria.join(' · ')}`));
        if ((proposal.project_constraints || []).length) projectContract.append(element('p', '', `Boundaries: ${proposal.project_constraints.join(' · ')}`));
        detailBody.append(projectContract);
      }
      const firstMove = element('div', 'proposal-first-move');
      firstMove.append(element('span', 'hq-kicker', proposal.creates_project ? 'FIRST BOUNDED MOVE' : 'BOUNDED COMPANY WORK'));
      firstMove.append(element('b', '', proposal.title));
      firstMove.append(element('p', '', proposal.objective));
      detailBody.append(firstMove);

      const team = element('div', 'proposal-team');
      (proposal.tasks || []).forEach((task) => {
        const row = element('article');
        row.append(element('b', '', task.title));
        row.append(element('p', '', task.objective));
        const capability = (task.required_capabilities || [])[0];
        row.append(element('small', '', [
          capability ? `needs ${capability}` : null,
          (task.write_paths || []).length ? `writes ${(task.write_paths || []).join(', ')}` : null,
          task.read_only_engineering ? 'read-only engineering' : null,
          `CEO proposes ${task.assignee}`,
          `review by ${task.reviewer}`,
          capability ? 'Company Runtime rematches the accountable specialist before execution' : null,
        ].filter(Boolean).join(' · ')));
        team.append(row);
      });
      detailBody.append(team);

      if ((proposal.staffing_gaps || []).length) {
        const staffing = element('div', 'proposal-project-contract');
        staffing.append(element('span', 'hq-kicker', 'COMPANY STAFFING'));
        staffing.append(element('b', '', 'Current roster has a real capability gap.'));
        staffing.append(element('p', '', proposal.staffing_gaps.map((gap) =>
          `${gap.capability} · governed HR assessment reserved NT$ ${gap.estimated_twd}`
        ).join(' · ')));
        detailBody.append(staffing);
      }
      if (proposal.engineering_readiness?.required) {
        const readiness = proposal.engineering_readiness;
        const engineering = element('div', 'proposal-project-contract');
        engineering.append(element('span', 'hq-kicker', 'ENGINEERING TOOL READINESS'));
        const label = readiness.verified
          ? 'Codex CLI verified ready'
          : readiness.ready
            ? 'Codex CLI has a cached ready snapshot; approval will re-verify it'
            : 'Codex CLI will be verified before Project authority is frozen';
        engineering.append(element('b', '', label));
        engineering.append(element('p', '', [
          readiness.transport ? `transport ${String(readiness.transport).toUpperCase()}` : null,
          readiness.version || null,
          readiness.repository ? `repo ${readiness.repository}` : null,
          (!readiness.ready && readiness.error) ? readiness.error : null,
          'Approve verifies CLI/login + governed Git repository only; it does not start an engineering job',
        ].filter(Boolean).join(' · ')));
        detailBody.append(engineering);
      }

      const meeting = proposal.meeting || {};
      const meetingBox = element('div', 'proposal-meeting');
      meetingBox.append(element('span', 'hq-kicker', 'MEETING ENVELOPE'));
      meetingBox.append(element('b', '', meeting.trigger === 'NEVER'
        ? 'No Meeting planned'
        : `${meeting.trigger} · ${meeting.max_rounds} round${meeting.max_rounds === 1 ? '' : 's'}`));
      const participants = (meeting.participants || []).join(', ') || 'CEO selects from the approved team';
      meetingBox.append(element('p', '', `${participants}. Token ceiling ${meeting.token_limit}; contribution cap ${meeting.contribution_output_cap}; retry ${meeting.retry_limit}; budget NT$ ${meeting.budget_twd}.`));
      detailBody.append(meetingBox);

      const criteria = element('ul', 'proposal-criteria');
      (proposal.completion_criteria || []).forEach((item) => criteria.append(element('li', '', item)));
      detailBody.append(criteria);
      if (proposal.open_url) {
        const open = element('a', 'dialogue-source-link', 'OPEN FULL AUDIT DETAILS →');
        open.href = proposal.open_url;
        detailBody.append(open);
      }
      details.append(summaryLabel, detailBody);
      card.append(details);
      return card;
    };

    const renderAnswer = (answer = {}, route = 'ADVISE') => {
      hideThinking();
      const message = appendMessage('CEO', answer.summary || answer.title || 'CEO response ready.', {
        runId: answer.run_id,
      });
      if (message && answer.proposal) message.append(proposalCard(answer.proposal));
      if (message && answer.href && !answer.proposal) {
        const link = element('a', 'dialogue-source-link', 'OPEN SOURCE CONTEXT →');
        link.href = answer.href;
        message.append(link);
      }
      return message;
    };

    const captureMetric = (payload) => {
      fetch('/headquarters/ceo/metrics', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
        keepalive: true,
      }).catch(() => {});
    };

    const showLivePanel = (state) => {
      if (!livePanel || !state) return;
      livePanel.hidden = false;
      livePanel.dataset.operationId = state.operation_id;
      qs('[data-operation-title]', livePanel).textContent = state.title;
      qs('[data-operation-status]', livePanel).textContent = state.status;
      qs('[data-operation-stage]', livePanel).textContent = state.stage || '';
      qs('[data-operation-progress]', livePanel).textContent = `${state.progress || 0}%`;
      qs('[data-operation-tasks]', livePanel).textContent = `${state.done || 0}/${state.total || 0} ${state.is_vnext ? 'Work' : 'tasks'}`;
      qs('[data-operation-remaining]', livePanel).textContent = state.remaining;
      qs('[data-operation-budget]', livePanel).textContent = state.budget;
      const open = qs('[data-operation-open]', livePanel);
      if (open) open.href = state.href;
      const resume = qs('[data-resume-operation]', livePanel);
      if (resume) {
        const canResume = Boolean(state.can_resume) || state.status === 'RUNNING';
        resume.hidden = !canResume || activeRunner === state.operation_id;
        resume.disabled = activeRunner === state.operation_id;
        resume.textContent = state.resume_label || 'RESUME CEO';
      }

      const workers = qs('[data-operation-workers]', livePanel);
      workers.replaceChildren();
      (state.workers || []).forEach((worker) => {
        const row = element('article');
        row.append(element('b', '', worker.name));
        row.append(element('span', '', worker.status));
        row.append(element('p', '', worker.task));
        const staffingMeta = [
          worker.required_capability ? `needs ${worker.required_capability}` : null,
          worker.staffing_state && worker.staffing_state !== worker.status ? `staffing ${worker.staffing_state}` : null,
        ].filter(Boolean).join(' · ');
        if (staffingMeta) row.append(element('small', '', staffingMeta));
        if (worker.wait_reason) row.append(element('small', '', worker.wait_reason));
        workers.append(row);
      });
      if (!workers.children.length) workers.append(element('p', 'operation-empty', 'No delivery Work is currently owned by an Employee.'));

      const event = qs('[data-operation-event]', livePanel);
      if (event) event.textContent = state.latest_event?.detail || state.waiting_reason || 'Waiting for the next governed step.';

      const meetingBox = qs('[data-operation-meeting]', livePanel);
      if (state.meeting) {
        meetingBox.hidden = false;
        qs('[data-meeting-title]', meetingBox).textContent = state.meeting.title;
        const summary = state.meeting.result?.agreement?.join?.(' ') ||
          state.meeting.summary?.agreement || state.meeting.summary?.open_question ||
          `Meeting ${state.meeting.status.toLowerCase()}.`;
        qs('[data-meeting-summary]', meetingBox).textContent = summary;
        qs('[data-meeting-usage]', meetingBox).textContent =
          `Round ${state.meeting.round}/${state.meeting.max_rounds} · ${state.meeting.tokens_used}/${state.meeting.token_limit} tokens · NT$ ${state.meeting.spent_twd}/${state.meeting.budget_twd} · retry limit ${state.meeting.retry_limit}`;
      } else {
        meetingBox.hidden = true;
      }

      const budgetBox = qs('[data-budget-request]', livePanel);
      budgetBox.replaceChildren();
      if (state.budget_request) {
        budgetBox.hidden = false;
        budgetBox.append(element('b', '', 'CEO requests additional budget'));
        budgetBox.append(element('p', '', state.budget_request.reason || 'The next bounded step exceeds the remaining budget.'));
        const button = element('button', 'hq-button', `AUTHORIZE NT$ ${state.budget_request.additional_twd}`);
        button.type = 'button';
        button.dataset.authorizeBudget = state.budget_request.additional_twd;
        button.dataset.operationId = state.operation_id;
        budgetBox.append(button);
      } else budgetBox.hidden = true;

      if (state.status === 'COMPLETED' && state.founder_report && finalReportShownFor !== state.operation_id) {
        finalReportShownFor = state.operation_id;
        appendMessage('CEO', `${state.founder_report.summary || state.founder_report.headline}\n\nResult: ${state.founder_report.result || 'Completed.'}\n\nBudget: NT$ ${state.spent} spent; NT$ ${state.remaining} remaining.`);
      }
    };

    const fetchOperationState = async (operationId) => {
      const response = await fetch(`/headquarters/ceo/operations/${operationId}/state`);
      const state = await response.json();
      if (!response.ok) throw new Error(state.error || 'Operation state could not be loaded.');
      showLivePanel(state);
      return state;
    };

    const runOperation = async (operationId, trigger = null) => {
      if (activeRunner === operationId) return;
      activeRunner = operationId;
      if (trigger) {
        trigger.disabled = true;
        trigger.textContent = 'STARTING CODEX…';
      }
      setDialogueState('CEO OPERATING');
      try {
        const started = await fetch(`/operations/${operationId}/runtime/start`, {
          method: 'POST', headers: {'Accept': 'application/json'},
        });
        const startPayload = await started.json();
        if (!started.ok) throw new Error(startPayload.error || 'CEO runtime could not start.');
        showLivePanel(startPayload.operation);
        while (activeRunner === operationId) {
          const state = await fetchOperationState(operationId);
          const runtimeState = state.runtime || {};
          if (state.status !== 'RUNNING' || (!runtimeState.worker_alive && runtimeState.state !== 'STARTING')) break;
          await new Promise((resolve) => window.setTimeout(resolve, 1200));
        }
      } catch (error) {
        appendMessage('CEO', `I paused the operation because the local runtime could not continue: ${error.message}`);
      } finally {
        activeRunner = null;
        const finalState = await fetchOperationState(operationId).catch(() => null);
        if (trigger) {
          trigger.disabled = false;
          trigger.textContent = finalState?.resume_label || 'RESUME CEO';
          trigger.hidden = !(finalState?.can_resume || finalState?.status === 'RUNNING');
        }
        setDialogueState(finalState?.status === 'WAITING_FOR_FOUNDER' ? 'NEEDS FOUNDER' : finalState?.status === 'COMPLETED' ? 'DELIVERY READY' : 'READY');
      }
    };

    thread?.addEventListener('click', async (event) => {
      const decline = event.target.closest('[data-decline-operation]');
      if (decline) {
        decline.disabled = true;
        decline.textContent = 'DECLINING…';
        const operationId = decline.dataset.declineOperation;
        try {
          const response = await fetch(`/headquarters/ceo/operations/${operationId}/decline`, {method: 'POST'});
          const payload = await response.json();
          if (!response.ok) throw new Error(payload.error || 'Decline failed.');
          appendMessage('CEO', payload.message || 'Declined. No Project execution authority was granted.');
          const card = decline.closest('.ceo-proposal-card');
          card?.classList.add('is-declined');
          qsa('button', card).forEach((button) => { button.disabled = true; });
          decline.textContent = 'DECLINED';
          setDialogueState('READY');
        } catch (error) {
          decline.disabled = false;
          decline.textContent = 'NO — DECLINE';
          appendMessage('CEO', `I could not decline the proposal: ${error.message}`);
        }
        return;
      }

      const approve = event.target.closest('[data-approve-operation]');
      if (!approve) return;
      approve.disabled = true;
      approve.textContent = 'APPROVING…';
      const operationId = approve.dataset.approveOperation;
      try {
        const response = await fetch(`/headquarters/ceo/operations/${operationId}/approve`, {method: 'POST'});
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || 'Approval failed.');
        const approvedMessage = appendMessage('CEO', 'Approved. Company Runtime owns the Project now. I will organize the approved Work, match the accountable Employees, and start every legal independent branch without another Founder start command.');
        if (approvedMessage && payload.operation?.href) {
          const projectLink = element('a', 'dialogue-source-link', 'WATCH THE COMPANY WORK →');
          projectLink.href = payload.operation.href;
          approvedMessage.append(projectLink);
        }
        showLivePanel(payload.operation);
        const card = approve.closest('.ceo-proposal-card');
        card?.classList.add('is-approved');
        qsa('button', card).forEach((button) => { button.disabled = true; });
        approve.textContent = 'APPROVED';
        // A newly authorized Project is now owned by Company Runtime. Move the
        // Founder to the Project/company surface instead of leaving them in the
        // old proposal/Mission control context while Employees start working.
        if (approve.dataset.createsProject === '1' && payload.operation?.href) {
          window.setTimeout(() => {
            const target = new URL(payload.operation.href, window.location.origin);
            target.searchParams.set('live', '1');
            window.location.assign(target.toString());
          }, 350);
        }
      } catch (error) {
        approve.disabled = false;
        approve.textContent = 'YES — RUN IT';
        appendMessage('CEO', `I could not approve the Project: ${error.message}`);
      }
    });


    livePanel?.addEventListener('click', async (event) => {
      const resume = event.target.closest('[data-resume-operation]');
      if (resume) {
        runOperation(Number(livePanel.dataset.operationId), resume);
        return;
      }
      const budget = event.target.closest('[data-authorize-budget]');
      if (!budget) return;
      budget.disabled = true;
      const operationId = budget.dataset.operationId;
      const amount = budget.dataset.authorizeBudget;
      try {
        const response = await fetch(`/headquarters/ceo/operations/${operationId}/budget`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({additional_budget_twd: amount}),
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || 'Budget authorization failed.');
        appendMessage('CEO', `Additional NT$ ${payload.additional_twd} authorized. I am resuming the operation.`);
        showLivePanel(payload.operation);
      } catch (error) {
        budget.disabled = false;
        appendMessage('CEO', `Budget authorization failed: ${error.message}`);
      }
    });

    officeForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      const requestText = textarea?.value.trim();
      if (!requestText) return;
      // Keep the last paid CEO HTTP request identity across a lost browser
      // response or page reload. A repeated identical Founder command reuses
      // the same server-side idempotency key instead of buying a second plan.
      const pendingRequestStorageKey = 'eason-one:ceo-pending-request-v1';
      let replayRequestId = null;
      try {
        const pending = JSON.parse(window.sessionStorage.getItem(pendingRequestStorageKey) || 'null');
        if (pending
            && pending.request === requestText
            && String(pending.project_id || '') === String(projectId || '')
            && pending.request_id) {
          replayRequestId = String(pending.request_id);
        }
      } catch (_error) {
        // sessionStorage is an optimization only; server authority remains DB-native.
      }
      const normalizedDecision = requestText.toLowerCase().replace(/[！!。．.]/g, '').trim();
      const pendingCards = qsa('.ceo-proposal-card:not(.is-approved):not(.is-declined)', thread);
      const pendingCard = pendingCards.length ? pendingCards[pendingCards.length - 1] : null;
      const yesWords = new Set(['yes', 'y', 'approve', 'ok', '可以', '好', '執行', '核准', '同意']);
      const noWords = new Set(['no', 'n', 'reject', 'decline', '不要', '不做', '拒絕', '取消']);
      if (pendingCard && (yesWords.has(normalizedDecision) || noWords.has(normalizedDecision))) {
        textarea.value = '';
        textarea.focus();
        appendMessage('FOUNDER', requestText);
        const selector = yesWords.has(normalizedDecision) ? '[data-approve-operation]' : '[data-decline-operation]';
        pendingCard.querySelector(selector)?.click();
        return;
      }
      textarea.value = '';
      textarea.focus();
      appendMessage('FOUNDER', requestText);
      setDialogueState('CEO THINKING');
      showThinking('Reading the company state and your intent…');
      const startedAt = performance.now();
      let firstUsefulAt = null;
      let requestId = replayRequestId;
      let route = 'AUTO';
      let runId = null;
      let previewServerMs = 0;
      let executionServerMs = 0;
      let success = false;

      runtime.hidden = false;
      setStep('received', 'active');
      routeLabel.textContent = 'AUTO';
      detailLabel.textContent = 'CEO is reading the current company state.';
      submitButton.disabled = true;
      timer = window.setInterval(() => {
        elapsedLabel.textContent = `${((performance.now() - startedAt) / 1000).toFixed(1)}s`;
      }, 100);

      try {
        const previewResponse = await fetch('/headquarters/ceo/preview', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({request: requestText, mode: 'AUTO', project_id: projectId}),
        });
        const preview = await previewResponse.json();
        if (!previewResponse.ok) throw new Error(preview.error || 'CEO preview failed.');
        requestId = replayRequestId || preview.request_id;
        route = preview.route;
        previewServerMs = preview.server_preview_ms || 0;
        routeLabel.textContent = route;
        setStep('received', 'done');
        setStep('snapshot', 'done');
        firstUsefulAt = performance.now();

        if (!preview.execute_required) {
          try { window.sessionStorage.removeItem(pendingRequestStorageKey); } catch (_error) {}
          renderAnswer(preview.immediate, route);
          setStep('judgment', 'done');
          setStep('complete', 'done');
          detailLabel.textContent = 'Answered from persisted company state; no provider call.';
          success = true;
        } else {
          setStep('judgment', 'active');
          detailLabel.textContent = route === 'ACT'
            ? 'CEO is selecting the team, budget, completion gates, and Meeting envelope.'
            : 'CEO is preparing a direct judgment.';
          showThinking(route === 'ACT'
            ? 'Choosing the smallest sufficient team, authority, and verification path…'
            : 'Forming a direct executive judgment…');
          try {
            window.sessionStorage.setItem(pendingRequestStorageKey, JSON.stringify({
              request: requestText, project_id: projectId || null, request_id: requestId,
            }));
          } catch (_error) {
            // The durable server request id still protects this live HTTP attempt.
          }
          const executionResponse = await fetch('/headquarters/ceo/execute', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({request: requestText, mode: route, request_id: requestId, project_id: projectId}),
          });
          const execution = await executionResponse.json();
          // A canonical JSON response means the server knows the outcome. Only a
          // transport loss keeps this key so the Founder can safely retry later.
          try { window.sessionStorage.removeItem(pendingRequestStorageKey); } catch (_error) {}
          executionServerMs = execution.server_total_ms || 0;
          runId = execution.run_id || execution.answer?.run_id || null;
          let answerRendered = false;
          if (execution.answer) {
            renderAnswer(execution.answer, route);
            answerRendered = true;
          }
          if (!executionResponse.ok) {
            const executionError = new Error(
              execution.error || execution.answer?.summary || 'CEO execution failed.'
            );
            executionError.answerRendered = answerRendered;
            throw executionError;
          }
          setStep('judgment', 'done');
          setStep('complete', 'done');
          detailLabel.textContent = execution.answer?.proposal
            ? 'A bounded proposal is ready for Founder approval.'
            : 'CEO reply is ready.';
          success = true;
        }
      } catch (error) {
        hideThinking();
        setStep('judgment', 'error');
        setStep('complete', 'error');
        // The server returns the canonical failure reply. Do not append a
        // second client-only wrapper that disappears after refresh.
        if (!error.answerRendered) {
          appendMessage('CEO', `I could not complete that request: ${error.message}`);
        }
        detailLabel.textContent = error.message || 'The CEO runtime failed.';
      } finally {
        hideThinking();
        window.clearInterval(timer);
        const finishedAt = performance.now();
        elapsedLabel.textContent = `${((finishedAt - startedAt) / 1000).toFixed(1)}s`;
        submitButton.disabled = false;
        setDialogueState('READY');
        captureMetric({
          request_id: requestId || `client-${Date.now()}`,
          route,
          first_useful_ms: firstUsefulAt ? firstUsefulAt - startedAt : finishedAt - startedAt,
          total_ms: finishedAt - startedAt,
          preview_server_ms: previewServerMs,
          execution_server_ms: executionServerMs,
          run_id: runId,
          success,
        });
      }
    });

    textarea?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        officeForm.requestSubmit();
      }
    });

    // Existing approved work is never resumed merely by opening HQ.
    // The Founder resumes it explicitly, while newly approved work starts
    // immediately from the approval action above.
  }

  // Project pages subscribe to the single global read pulse; they never create
  // a second polling loop or advance company work from the browser.
  const projectLiveRoot = qs('[data-project-live-root]');
  if (projectLiveRoot) {
    const projectId = Number(projectLiveRoot.dataset.projectId || 0);
    const signal = qs('[data-project-signal]', projectLiveRoot);
    const stateLabel = qs('[data-project-state-label]', projectLiveRoot);
    const stateDetail = qs('[data-project-state-detail]', projectLiveRoot);
    const durableState = String(projectLiveRoot.dataset.projectState || '').toUpperCase();
    const durableGovernanceStates = new Set(['WAITING', 'RECOVERING', 'PAUSED', 'VERIFYING', 'NEEDS_YOU', 'RESULT_READY', 'COMPLETED', 'CANCELLED', 'FAILED', 'BLOCKED']);
    let sawLiveWork = false;
    window.addEventListener('eason:runtime-focus', (event) => {
      const payload = event.detail || {};
      const isThisProject = Boolean(payload.active && Number(payload.project_id) === projectId);
      // A transient runtime pulse may enrich an executable Project, but it may
      // never overwrite higher-priority durable Project/Founder truth. The
      // server read model owns NEEDS_YOU/RESULT_READY/terminal/blocking states.
      if (isThisProject && !durableGovernanceStates.has(durableState)) {
        sawLiveWork = true;
        projectLiveRoot.dataset.projectState = 'WORKING';
        if (signal) signal.className = 'v013-project-signal state-working';
        if (stateLabel) stateLabel.textContent = 'WORKING';
        if (stateDetail) stateDetail.textContent = [payload.employee, payload.current_action].filter(Boolean).join(' · ') || 'Company work is live.';
      } else if (sawLiveWork) {
        sawLiveWork = false;
        if (stateLabel) stateLabel.textContent = 'UPDATING';
        if (stateDetail) stateDetail.textContent = 'A bounded step finished. Refreshing durable Project truth…';
        window.setTimeout(() => window.location.reload(), 700);
      }
    });
  }

})();
