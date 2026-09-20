(() => {
  const root = document.documentElement;
  const toggle = document.querySelector("[data-sidebar-toggle]");
  const key = "eason-one.sidebar-collapsed";
  const apply = collapsed => {
    root.classList.toggle("sidebar-collapsed", collapsed);
    if (toggle) {
      toggle.setAttribute("aria-expanded", String(!collapsed));
      toggle.setAttribute(
        "aria-label", collapsed ? "Expand sidebar" : "Collapse sidebar");
      toggle.textContent = collapsed ? "›" : "‹";
    }
  };
  apply(localStorage.getItem(key) === "1");
  toggle?.addEventListener("click", () => {
    const collapsed = !root.classList.contains("sidebar-collapsed");
    localStorage.setItem(key, collapsed ? "1" : "0");
    apply(collapsed);
  });
})();
