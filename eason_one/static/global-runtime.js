(() => {
  "use strict";
  const strip = document.querySelector("[data-global-runtime]");
  if (!strip) return;
  const title = strip.querySelector("[data-global-runtime-title]");
  const meta = strip.querySelector("[data-global-runtime-meta]");
  const link = strip.querySelector("[data-global-runtime-link]");
  const state = strip.querySelector("[data-global-runtime-state]");
  let failures = 0;

  const label = (value) => String(value || "").replaceAll("_", " ");
  const founderAction = (payload) => {
    const phase = String(payload?.phase || "").toUpperCase();
    const copy = {
      INSPECT: "Inspecting the repository and task",
      PREPARE: "Preparing the bounded implementation",
      IMPLEMENT: "Implementing the approved change",
      VERIFY: "Verifying the implementation on the host",
      DELIVER: "Persisting the artifact and delivery evidence",
    };
    if (copy[phase]) return copy[phase];
    const raw = String(payload?.current_action || "").trim();
    if (/^(\/?bin\/)?(bash|sh)\b|^timeout\b|^env\b|^python\d*\b|^git\b/i.test(raw)) {
      return "Running a bounded repository command";
    }
    return raw;
  };
  const render = (payload) => {
    window.dispatchEvent(new CustomEvent("eason:runtime-focus", {detail: payload || {active:false}}));
    if (!payload?.active) {
      const queued = payload?.queued_work;
      if (!queued) {
        strip.hidden = true;
        return;
      }
      failures = 0;
      strip.hidden = false;
      title.textContent = queued.project_title || "Company work is queued";
      state.textContent = label(queued.status || "READY");
      meta.textContent = [queued.employee, queued.title, "Company Runtime owns the next move"]
        .filter(Boolean).join(" · ");
      link.href = queued.href || "/headquarters/projects";
      return;
    }
    failures = 0;
    strip.hidden = false;
    title.textContent = payload.title || "Company work is running";
    state.textContent = label(payload.phase || payload.status || "WORKING");
    meta.textContent = [payload.employee, payload.task, founderAction(payload)]
      .filter(Boolean).join(" · ");
    link.href = payload.href || "/headquarters/projects";
  };

  const poll = async () => {
    try {
      const response = await fetch("/api/headquarters/runtime-focus", {
        headers: {Accept: "application/json"}, cache: "no-store",
      });
      if (!response.ok) throw new Error(`runtime ${response.status}`);
      render(await response.json());
    } catch (_) {
      failures += 1;
      if (failures > 2) strip.hidden = true;
    } finally {
      window.setTimeout(poll, document.hidden ? 15000 : 5000);
    }
  };
  poll();
})();
