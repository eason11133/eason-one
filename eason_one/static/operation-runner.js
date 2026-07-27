(() => {
  for (const runner of document.querySelectorAll("[data-operation-runner]")) {
    const operationId = runner.dataset.operationId;
    const endpoint = `/operations/${operationId}/next-step`;
    const completionUrl =
      runner.dataset.completionUrl || `/operations/${operationId}`;
    const prefix = `operation-${operationId}-${Date.now()}-${Math.random()
      .toString(16)
      .slice(2)}`;
    let sequence = 0;
    let stopped = false;

    async function advance() {
      if (stopped) return;
      const response = await fetch(endpoint, {
        method: "POST",
        headers: {"Idempotency-Key": `${prefix}-${sequence}`},
      });
      const result = await response.json();
      if (!response.ok) {
        runner.textContent = result.error || "CEO operation paused.";
        return;
      }
      sequence += 1;
      runner.textContent = `CEO completed ${result.kind}.`;
      if (result.continue_allowed) {
        advance();
      } else {
        window.location.replace(completionUrl);
      }
    }

    window.addEventListener("beforeunload", () => {
      stopped = true;
    });
    advance();
  }
})();
