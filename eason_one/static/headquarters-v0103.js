(() => {
  "use strict";

  const normalize = (value) => String(value || "").replace(/\s+/g, " ").trim();
  const headings = () => [...document.querySelectorAll("h1,h2,h3,[class*='title'],[class*='heading']")];

  function textNodeMatch(needles) {
    const wanted = needles.map((item) => item.toLowerCase());
    return headings().find((node) => {
      const text = normalize(node.textContent).toLowerCase();
      return wanted.some((needle) => text.includes(needle));
    }) || null;
  }

  function nearestCard(node, boundary) {
    let current = node;
    let fallback = null;
    while (current && current !== boundary && current !== document.body) {
      const className = typeof current.className === "string" ? current.className.toLowerCase() : "";
      if (current.matches("section,article,aside") || /(card|panel|tile|module|surface|widget|block)/.test(className)) {
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
    cards.filter((existing) => card.contains(existing)).forEach((child) => cards.splice(cards.indexOf(child), 1));
    cards.push(card);
    return card;
  }

  function cardFor(needles, main, cards) {
    return uniqueCard(cards, nearestCard(textNodeMatch(needles), main));
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

  function briefingCopy(briefing) {
    if (!briefing) return "The CEO is ready to turn Founder intent into governed, auditable company work.";
    const recommendation = briefing.querySelector(".ceo-recommendation");
    const title = normalize(recommendation?.querySelector("h3")?.textContent);
    const reason = normalize(recommendation?.querySelector("p")?.textContent);
    if (title && reason) return `${title}. ${reason}`;
    if (title) return title;
    const fallback = briefing.cloneNode(true);
    fallback.querySelectorAll("a,button,form,time,script,style").forEach((node) => node.remove());
    return normalize(fallback.textContent).replace(/^CEO briefing\s*/i, "").slice(0, 420) ||
      "The CEO is ready to turn Founder intent into governed, auditable company work.";
  }

  function bridgeCeoState(root, heroState, directCard) {
    const native = directCard?.querySelector("[data-dialogue-state]");
    if (!native) return;
    const sync = () => {
      const value = normalize(native.textContent) || "READY";
      heroState.textContent = value.replace(/^CEO\s+/i, "");
      const lowered = value.toLowerCase();
      root.dataset.ceoState = lowered.includes("thinking") || lowered.includes("operating")
        ? "thinking"
        : lowered.includes("fail") || lowered.includes("error")
          ? "failure"
          : lowered.includes("founder")
            ? "founder_decision"
            : "normal_response";
    };
    sync();
    new MutationObserver(sync).observe(native, {childList: true, characterData: true, subtree: true});
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

    if (!direct || !briefing) return;

    main.classList.add("v010-hq-host", "v010-expand");
    let parent = main.parentElement;
    for (let depth = 0; parent && parent !== document.body && depth < 3; depth += 1) {
      parent.classList.add("v010-expand");
      parent = parent.parentElement;
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
    ceoCopy.append(labels, create("p", "v010-briefing-copy", briefingCopy(briefing)));
    ceoStage.append(orbWrap, ceoCopy);

    const attentionRail = create("aside", "v010-attention-rail");
    const attentionHead = create("div", "v010-attention-heading");
    const attentionCopy = create("div");
    attentionCopy.append(create("p", "v010-eyebrow", "Decision surface"), create("h2", "", "Needs your attention"));
    const count = normalize(attention?.querySelector(":scope > header b")?.textContent);
    attentionHead.append(attentionCopy);
    if (count) attentionHead.append(create("span", "v010-meta-pill", `${count} waiting`));
    attentionRail.append(attentionHead);
    const attentionSlot = create("div", "v010-attention-slot");
    if (attention) {
      attention.classList.add("v010-native-heading-hidden");
      attentionSlot.append(attention);
    } else {
      attentionSlot.append(create("div", "v010-empty-note", "No Founder decision is waiting."));
    }
    attentionRail.append(attentionSlot);
    hero.append(ceoStage, attentionRail);

    direct.classList.add("v010-native-dialogue");
    const line = create("section", "v010-direct-line");
    const lineHead = create("div", "v010-direct-line-head");
    const lineTitle = create("div");
    lineTitle.append(create("p", "v010-eyebrow", "CEO direct line"), create("h2", "", "Give one bounded task"));
    lineHead.append(lineTitle, create("span", "v010-meta-pill", "Authoritative Run path"));
    const directSlot = create("div", "v010-direct-line-slot");
    directSlot.append(direct);
    line.append(lineHead, directSlot);

    const operatingGrid = create("section", "v010-operating-grid");
    const missionSurface = surface("Current mission", mission);
    if (missionSurface) operatingGrid.append(missionSurface);
    const secondaryRail = create("div", "v010-secondary-rail");
    const activitySurface = surface("Live company activity", activity);
    const teamSurface = surface("CEO team", team);
    if (activitySurface) secondaryRail.append(activitySurface);
    if (teamSurface) secondaryRail.append(teamSurface);
    if (secondaryRail.children.length) operatingGrid.append(secondaryRail);

    root.append(hero, line);
    if (operatingGrid.children.length) root.append(operatingGrid);
    main.prepend(root);
    briefing.remove();
    bridgeCeoState(root, stateText, direct);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", buildHeadquarters, {once: true});
  } else {
    buildHeadquarters();
  }
})();
