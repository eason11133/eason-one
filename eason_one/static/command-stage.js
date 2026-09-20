(() => {
  document.querySelectorAll("[data-budget-additional]").forEach(input => {
    const output = input.form?.querySelector("[data-budget-result]");
    const render = () => {
      const total = Number(input.dataset.currentTotal) + Number(input.value);
      if (output) output.textContent = Number.isFinite(total)
        ? total.toFixed(2) : "—";
    };
    input.addEventListener("input", render);
    render();
  });
  const stage = document.querySelector(".ceo-canvas");
  const composer = document.querySelector("[data-ceo-composer]");
  if (!stage || !composer) return;
  composer.addEventListener("submit", () => {
    stage.classList.add("is-thinking");
    stage.dataset.stageState = "thinking";
    composer.querySelector("button").disabled = true;
  });
  const editors = [...stage.querySelectorAll(
    ".founder-decision-actions details")];
  editors.forEach(editor => {
    editor.addEventListener("toggle", () => {
      if (editor.open) {
        editors.forEach(other => {
          if (other !== editor) other.open = false;
        });
      }
      stage.classList.toggle(
        "decision-editor-open", editors.some(item => item.open));
    });
    editor.querySelector("[data-close-editor]")?.addEventListener(
      "click", () => { editor.open = false; });
  });
})();
