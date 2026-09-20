(() => {
  "use strict";

  const sleep = (ms) => new Promise((resolve) => window.setTimeout(resolve, ms));
  const phaseOrder = [
    "JOB_SPEC_PREPARED", "STARTING_CODEX", "CODEX_SESSION_STARTED",
    "ANALYZING_TASK", "READING_REPOSITORY", "RUNNING_COMMAND",
    "APPLYING_CHANGES", "RUNNING_TESTS", "SYNTHESIZING_RESULT",
    "VALIDATING_OUTPUT", "EVIDENCE_PERSISTED",
  ];

  const formatDuration = (seconds) => {
    if (seconds === null || seconds === undefined) return "—";
    const value = Math.max(0, Number(seconds) || 0);
    if (value < 60) return `${value}s`;
    const minutes = Math.floor(value / 60);
    const remainder = value % 60;
    if (minutes < 60) return `${minutes}m ${remainder}s`;
    return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
  };

  const escapeHtml = (value) => String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");

  for (const monitor of document.querySelectorAll("[data-operation-monitor]")) {
    const operationId = Number(monitor.dataset.operationId);
    const shouldStart = monitor.hasAttribute("data-operation-runner");
    const stateEndpoint = `/operations/${operationId}/runtime`;
    const startEndpoint = `/operations/${operationId}/runtime/start`;
    const title = monitor.querySelector("[data-runner-title]");
    const detail = monitor.querySelector("[data-runner-detail]");
    const stateLabel = monitor.querySelector("[data-runner-state]");
    const activityLabel = monitor.querySelector("[data-runtime-activity-state]");
    const stopButton = monitor.querySelector("[data-runner-stop]");
    const resumeButton = monitor.querySelector("[data-runtime-resume]");
    const employeeLabel = monitor.querySelector("[data-runtime-employee]");
    const runLabel = monitor.querySelector("[data-runtime-run]");
    const planningRunLabel = monitor.querySelector("[data-runtime-planning-run]");
    const phaseLabel = monitor.querySelector("[data-runtime-phase]");
    const actionLabel = monitor.querySelector("[data-runtime-action]");
    const elapsedLabel = monitor.querySelector("[data-runtime-elapsed]");
    const lastActivityLabel = monitor.querySelector("[data-runtime-last-activity]");
    const commandsLabel = monitor.querySelector("[data-runtime-commands]");
    const testsLabel = monitor.querySelector("[data-runtime-tests]");
    const filesLabel = monitor.querySelector("[data-runtime-files]");
    const eventList = monitor.querySelector("[data-runtime-events]");
    const eventCount = monitor.querySelector("[data-runtime-event-count]");
    const timeline = monitor.querySelector("[data-runtime-timeline]");
    const costLabels = document.querySelectorAll("[data-runtime-cost]");
    const remainingLabels = document.querySelectorAll("[data-runtime-remaining]");
    const companySpentLabels = document.querySelectorAll("[data-runtime-company-spent]");
    let lastSignature = "";
    let runnerActive = false;

    const setText = (node, value) => {
      if (!node) return;
      const next = value ?? "—";
      if (node.textContent !== next) node.textContent = next;
    };

    const renderTimeline = (currentPhase, terminalState) => {
      if (!timeline) return;
      const currentIndex = phaseOrder.indexOf(currentPhase);
      for (const stage of timeline.querySelectorAll("[data-runtime-stage]")) {
        const members = String(stage.dataset.runtimeStage || "").split(",");
        const memberIndexes = members.map((name) => phaseOrder.indexOf(name)).filter((value) => value >= 0);
        const first = memberIndexes.length ? Math.min(...memberIndexes) : -1;
        const last = memberIndexes.length ? Math.max(...memberIndexes) : -1;
        const active = members.includes(currentPhase) && !["COMPLETED", "ARCHIVED", "FAILED"].includes(terminalState);
        const done = terminalState === "COMPLETED" || terminalState === "ARCHIVED" || (currentIndex >= 0 && last >= 0 && last < currentIndex);
        stage.classList.toggle("is-active", active);
        stage.classList.toggle("is-done", done);
      }
      timeline.dataset.currentPhase = currentPhase || "";
    };

    const renderEvents = (events) => {
      if (!eventList) return;
      const rows = Array.isArray(events) ? events.slice(-6).reverse() : [];
      setText(eventCount, `${rows.length} EVENTS`);
      if (!rows.length) {
        eventList.innerHTML = '<div class="signal-empty">No live execution event has been recorded yet.</div>';
        return;
      }
      eventList.innerHTML = rows.map((event) => {
        const time = String(event.at || "").slice(11, 19) || "--:--:--";
        return `<div class="runtime-event"><time>${escapeHtml(time)}</time><span>${escapeHtml(event.kind || "EVENT")}</span><p>${escapeHtml(event.detail || "Runtime activity recorded.")}</p></div>`;
      }).join("");
    };

    const render = (payload) => {
      const runtime = payload.runtime || {};
      const operation = payload.operation || {};
      const live = runtime.live_execution || {};
      const counters = live.counters || {};
      const task = runtime.active_task || operation.current_task;
      const executionRun = runtime.execution_run || runtime.latest_execution_run;
      const planningRun = runtime.planning_run;
      const state = runtime.state || operation.status || "READY";
      // Clock fields change even when no signed business state changes. Update
      // them before the signature guard so elapsed/heartbeat never freeze.
      setText(elapsedLabel, formatDuration(live.elapsed_seconds));
      setText(lastActivityLabel, live.last_activity_seconds === null || live.last_activity_seconds === undefined ? "—" : `${formatDuration(live.last_activity_seconds)} ago`);
      const signature = JSON.stringify({
        state,
        operationStatus: operation.status,
        workerAlive: runtime.worker_alive,
        taskId: task?.id || null,
        executionRunId: executionRun?.id || null,
        executionRunStatus: executionRun?.status || null,
        phase: live.phase || null,
        action: live.current_action || null,
        lastActivity: live.last_activity_at || null,
        eventCount: live.events?.length || 0,
        counters,
        spent: operation.spent || "0",
        remaining: operation.remaining || "0",
        companySpent: operation.company_spent || "0",
        waitingReason: operation.waiting_reason || null,
        canResume: runtime.can_resume || false,
        archived: runtime.archived || operation.archived || false,
      });
      if (signature === lastSignature) return;
      lastSignature = signature;

      setText(stateLabel, state.replaceAll("_", " "));
      setText(activityLabel, (live.activity_state || (runtime.worker_alive ? "ACTIVE" : "IDLE")).replaceAll("_", " "));
      setText(employeeLabel, task?.employee || executionRun?.employee || "No active Employee");
      setText(runLabel, executionRun ? `#${executionRun.id} · ${executionRun.status}` : "None");
      setText(planningRunLabel, planningRun ? `Planning Run #${planningRun.id} is complete and separate from Employee execution.` : "No planning Run is linked to this Mission.");
      setText(phaseLabel, (live.phase || state || "IDLE").replaceAll("_", " "));
      setText(actionLabel, live.current_action || "No active command");
      setText(elapsedLabel, formatDuration(live.elapsed_seconds));
      setText(lastActivityLabel, live.last_activity_seconds === null || live.last_activity_seconds === undefined ? "—" : `${formatDuration(live.last_activity_seconds)} ago`);
      setText(commandsLabel, `${counters.commands_completed || 0}/${counters.commands_started || 0}`);
      setText(testsLabel, `${counters.tests_completed || 0}/${counters.tests_started || 0}`);
      setText(filesLabel, String((counters.files_changed || []).length));
      for (const node of costLabels) setText(node, `NT$ ${operation.spent ?? "0"}`);
      for (const node of remainingLabels) setText(node, `NT$ ${operation.remaining ?? "0"}`);
      for (const node of companySpentLabels) setText(node, `NT$ ${operation.company_spent ?? "0"}`);
      renderTimeline(
        ["FAILED", "ARCHIVED"].includes(state) ? (live.last_progress_phase || live.phase) : live.phase,
        state,
      );
      renderEvents(live.events);

      if (resumeButton) {
        const canResume = Boolean(runtime.can_resume || operation.can_resume);
        resumeButton.hidden = !canResume || Boolean(runtime.worker_alive) || Boolean(runtime.archived);
        resumeButton.disabled = runnerActive || !canResume;
        setText(resumeButton, runnerActive ? "STARTING CODEX…" : (operation.resume_label || "RESUME ENGINEER → CODEX"));
      }
      if (stopButton) stopButton.disabled = !runtime.worker_alive;

      if (runtime.archived || operation.archived || state === "ARCHIVED") {
        setText(title, "Runtime validation archived.");
        setText(detail, operation.waiting_reason || live.detail || "No further Employee or Provider work can start.");
      } else if (state === "WORKING" || state === "STARTING") {
        setText(title, task?.employee ? `${task.employee} is working.` : "CEO runtime is advancing the Mission.");
        setText(detail, live.detail || (task ? `${task.title} · ${task.status}` : "Preparing the next governed step."));
      } else if (state === "WAITING_FOR_BUDGET") {
        setText(title, "Execution is waiting for Founder budget authority.");
        setText(detail, operation.waiting_reason || "No Employee Run will start without Founder approval.");
      } else if (operation.status === "WAITING_FOR_FOUNDER" || state === "NEEDS_FOUNDER") {
        setText(title, "Execution is paused at a Founder gate.");
        setText(detail, operation.waiting_reason || runtime.last_error || "Founder authority is required before the next step.");
      } else if (operation.status === "COMPLETED" || state === "COMPLETED") {
        setText(title, "Mission completed and ready for Founder delivery.");
        setText(detail, operation.founder_report?.summary || live.detail || operation.latest_event?.detail || "The governed workflow is complete.");
      } else if (operation.status === "PAUSED" || state === "PAUSED") {
        setText(title, "CEO runtime is paused.");
        setText(detail, operation.waiting_reason || live.detail || "No Employee Run is active.");
      } else if (operation.status === "FAILED" || state === "FAILED") {
        setText(title, "CEO runtime stopped safely after a failure.");
        setText(detail, runtime.last_error || live.detail || operation.latest_event?.detail || "Review the failure before resuming.");
      } else {
        setText(title, "Mission is approved; CEO runtime is ready.");
        setText(detail, runtime.last_error || live.detail || operation.latest_event?.detail || "No Employee Run is active.");
      }
    };

    const read = async () => {
      const response = await fetch(stateEndpoint, {headers: {Accept: "application/json"}, cache: "no-store"});
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "Runtime state could not be loaded.");
      render(payload);
      return payload;
    };

    const start = async () => {
      const response = await fetch(startEndpoint, {method: "POST", headers: {Accept: "application/json"}});
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "CEO runtime could not start.");
      render(payload);
      return payload;
    };

    const monitorUntilGate = async (startNow) => {
      if (runnerActive) return;
      runnerActive = true;
      if (resumeButton) {
        resumeButton.disabled = true;
        resumeButton.hidden = false;
        setText(resumeButton, "STARTING CODEX…");
      }
      try {
        let payload = startNow ? await start() : await read();
        while (document.body.contains(monitor)) {
          const status = payload.operation?.status;
          const alive = Boolean(payload.runtime?.worker_alive);
          if (!alive && status !== "RUNNING") break;
          await sleep(alive ? 900 : 700);
          payload = await read();
        }
      } catch (error) {
        setText(stateLabel, "PAUSED");
        setText(title, "CEO runtime stopped safely.");
        setText(detail, error.message || String(error));
      } finally {
        runnerActive = false;
        lastSignature = "";
        await read().catch(() => null);
      }
    };

    stopButton?.addEventListener("click", async () => {
      stopButton.disabled = true;
      setText(stateLabel, "PAUSING");
      setText(title, "The current tool call may finish; no new step will begin.");
      try {
        await fetch(`/operations/${operationId}/pause`, {method: "POST"});
        await read();
      } finally {
        stopButton.disabled = false;
      }
    });

    resumeButton?.addEventListener("click", () => monitorUntilGate(true));
    monitorUntilGate(shouldStart);
  }
})();
