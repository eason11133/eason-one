(() => {
  "use strict";

  const normalize = (value) => (value || "").replace(/\s+/g, " ").trim().toLowerCase();

  function textNodeMatch(needles) {
    const wanted = needles.map(normalize);
    const candidates = Array.from(document.querySelectorAll("h1,h2,h3,h4,[class*='title'],[class*='label'],strong"));
    return candidates.find((node) => {
      const text = normalize(node.textContent);
      return wanted.some((needle) => text === needle || text.includes(needle));
    }) || null;
  }

  function nearestCard(node, boundary) {
    if (!node) return null;
    let current = node;
    let fallback = node.parentElement;
    while (current && current !== boundary && current !== document.body) {
      const className = typeof current.className === "string" ? current.className.toLowerCase() : "";
      if (
        current.matches("section,article") ||
        /(card|panel|tile|module|surface|widget|block)/.test(className)
      ) {
        return current;
      }
      fallback = current;
      current = current.parentElement;
    }
    return fallback && fallback !== boundary ? fallback : null;
  }

  function uniqueCard(cards, card) {
    if (!card || cards.includes(card)) return null;
    if (cards.some((existing) => existing.contains(card))) return null;
    const children = cards.filter((existing) => card.contains(existing));
    children.forEach((child) => cards.splice(cards.indexOf(child), 1));
    cards.push(card);
    return card;
  }

  function cardFor(needles, main, cards) {
    const heading = textNodeMatch(needles);
    const card = nearestCard(heading, main);
    return uniqueCard(cards, card);
  }

  function meaningfulText(card, excluded) {
    if (!card) return "";
    const clone = card.cloneNode(true);
    clone.querySelectorAll("form,button,input,textarea,script,style").forEach((node) => node.remove());
    const text = clone.textContent.replace(/\s+/g, " ").trim();
    const result = excluded.reduce((value, word) => value.replace(new RegExp(word, "ig"), ""), text);
    return result.replace(/\s+/g, " ").trim();
  }

  function create(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function surface(title, card) {
    if (!card) return null;
    const wrapper = create("section", "v010-surface");
    const head = create("div", "v010-surface-head");
    head.append(create("h2", "", title));
    const slot = create("div", "v010-surface-slot");
    slot.append(card);
    wrapper.append(head, slot);
    return wrapper;
  }

  function strictDeterministicStatusQuery(prompt) {
    const text = normalize(prompt);
    const planningSignals = /(plan|prepare|review|analyse|analyze|recommend|propose|design|develop|build|improve|fix|risk|acceptance|criteria|participate|three-step|next task|下一步|規劃|建議|分析|改善|修正|開發|設計|風險|驗收)/i;
    if (planningSignals.test(text)) return false;
    const narrowStatus = /^(how many|what is the current|show current|current status|status of|目前有幾|現在有幾|目前狀態|目前成本|目前預算|現在狀態)/i;
    return narrowStatus.test(text) && text.length < 180;
  }

  function findDirectLineForm(card) {
    if (!card) return null;
    const forms = Array.from(card.querySelectorAll("form"));
    return forms.find((form) => form.querySelector("textarea,input[type='text']")) || null;
  }

  function runMetaLine(run) {
    const line = create("div", "v010-run-line");
    if (!run) return line;
    const values = [
      run.id ? `RUN #${run.id}` : null,
      run.status || null,
      run.provider || null,
      run.model || null,
      run.input_tokens != null || run.output_tokens != null
        ? `${run.input_tokens ?? "?"} → ${run.output_tokens ?? "?"} tokens`
        : null,
      run.latency_ms != null ? `${run.latency_ms} ms` : null,
      run.cost != null ? `Cost ${run.cost}` : null,
    ].filter(Boolean);
    values.forEach((value) => line.append(create("span", "", String(value))));
    return line;
  }

  function appendMessage(thread, role, text, run) {
    const message = create("div", `v010-message v010-message--${role}`);
    message.append(create("span", "v010-message-label", role === "founder" ? "Founder" : "CEO"));
    message.append(document.createTextNode(text));
    if (run) message.append(runMetaLine(run));
    thread.append(message);
    thread.scrollTop = thread.scrollHeight;
  }

  function installRuntime(root, directCard, thread, stateText) {
    const form = findDirectLineForm(directCard);
    if (!form || form.dataset.v010Bound === "1") return;
    form.dataset.v010Bound = "1";
    const input = form.querySelector("textarea,input[type='text']");
    const submit = form.querySelector("button[type='submit'],input[type='submit']");

    form.addEventListener("submit", async (event) => {
      const prompt = (input?.value || "").trim();
      if (!prompt) return;

      // Keep the existing deterministic snapshot route for narrow factual status queries.
      if (strictDeterministicStatusQuery(prompt)) return;

      event.preventDefault();
      event.stopImmediatePropagation();
      root.dataset.ceoState = "thinking";
      stateText.textContent = "Thinking";
      if (submit) submit.disabled = true;
      appendMessage(thread, "founder", prompt);
      if (input) input.value = "";

      try {
        const formData = new FormData(form);
        formData.set("prompt", prompt);
        const response = await fetch("/headquarters/v0102/ceo", {
          method: "POST",
          body: formData,
          headers: { "X-Requested-With": "XMLHttpRequest" },
          credentials: "same-origin",
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok || !payload.ok) {
          throw new Error(payload.error || payload.failure_code || `HTTP ${response.status}`);
        }
        appendMessage(thread, "ceo", payload.executive_response || "CEO Run completed.", payload.run);
        root.dataset.ceoState = "normal_response";
        stateText.textContent = "Ready";
      } catch (error) {
        appendMessage(thread, "ceo", `Runtime failure: ${error.message}`, null);
        root.dataset.ceoState = "failure";
        stateText.textContent = "Failure";
      } finally {
        if (submit) submit.disabled = false;
      }
    }, { capture: true });
  }

  function buildHeadquarters() {
    if (document.querySelector(".v010-hq-root")) return;
    const main = document.querySelector("main,[role='main']") || document.body;
    if (!main) return;

    const cards = [];
    const briefing = cardFor(["CEO briefing"], main, cards);
    const attention = cardFor(["Needs your attention"], main, cards);
    const direct = cardFor(["Talk to your CEO", "CEO direct line"], main, cards);
    const mission = cardFor(["Current mission"], main, cards);
    const activity = cardFor(["Live company activity", "Recent company activity"], main, cards);
    const team = cardFor(["My CEO team", "CEO team", "Team on mission"], main, cards);

    // This script is page-specific. Do not alter other Headquarters rooms.
    if (!direct || !briefing) return;

    main.classList.add("v010-hq-host", "v010-expand");
    let parent = main.parentElement;
    let depth = 0;
    while (parent && parent !== document.body && depth < 3) {
      parent.classList.add("v010-expand");
      parent = parent.parentElement;
      depth += 1;
    }

    const root = create("div", "v010-hq-root");
    root.dataset.ceoState = "idle";

    const hero = create("section", "v010-hero");
    const ceoStage = create("div", "v010-ceo-stage");
    const orbWrap = create("div", "v010-orb-wrap");
    orbWrap.append(create("div", "v010-orb"));
    const ceoCopy = create("div", "v010-ceo-copy");
    ceoCopy.append(create("p", "v010-eyebrow", "Founder Headquarters · CEO on site"));
    ceoCopy.append(create("h1", "v010-title", "CEO Office"));
    const labels = create("div", "v010-ceo-label");
    const statePill = create("span", "v010-state-pill");
    const stateText = create("span", "", "Ready");
    statePill.append(stateText);
    labels.append(statePill, create("span", "v010-meta-pill", "Eason One"));
    ceoCopy.append(labels);
    const briefingText = meaningfulText(briefing, ["CEO briefing", "No authoritative change has been recorded"]);
    ceoCopy.append(create(
      "p",
      "v010-briefing-copy",
      briefingText || "The CEO is ready to turn Founder intent into governed, auditable company work."
    ));
    ceoStage.append(orbWrap, ceoCopy);

    const attentionRail = create("aside", "v010-attention-rail");
    const attentionHead = create("div", "v010-attention-heading");
    attentionHead.append(create("div", "", ""));
    attentionHead.firstElementChild.append(
      create("p", "v010-eyebrow", "Decision surface"),
      create("h2", "", "Needs your attention")
    );
    attentionRail.append(attentionHead);
    const attentionSlot = create("div", "v010-attention-slot");
    if (attention) attentionSlot.append(attention);
    else attentionSlot.append(create("div", "v010-empty-note", "No Founder decision is waiting."));
    attentionRail.append(attentionSlot);
    hero.append(ceoStage, attentionRail);

    const line = create("section", "v010-direct-line");
    const lineHead = create("div", "v010-direct-line-head");
    const lineTitle = create("div", "");
    lineTitle.append(create("p", "v010-eyebrow", "CEO direct line"), create("h2", "", "Give one bounded task"));
    lineHead.append(lineTitle, create("span", "v010-meta-pill", "Run-backed"));
    const thread = create("div", "v010-thread");
    const directSlot = create("div", "v010-direct-line-slot");
    directSlot.append(direct);
    line.append(lineHead, thread, directSlot);

    const operatingGrid = create("section", "v010-operating-grid");
    const missionSurface = surface("Current mission", mission);
    const activitySurface = surface("Live company activity", activity);
    const teamSurface = surface("CEO team", team);
    if (missionSurface) operatingGrid.append(missionSurface);
    const rail = create("div", "v010-secondary-rail");
    rail.style.display = "grid";
    rail.style.gap = "22px";
    if (activitySurface) rail.append(activitySurface);
    if (teamSurface) rail.append(teamSurface);
    if (rail.children.length) operatingGrid.append(rail);

    root.append(hero, line);
    if (operatingGrid.children.length) root.append(operatingGrid);
    main.prepend(root);
    briefing.remove();

    installRuntime(root, direct, thread, stateText);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", buildHeadquarters, { once: true });
  } else {
    buildHeadquarters();
  }
})();
