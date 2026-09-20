"use strict";
const path = require("path");

class NodeStub {
  constructor(text = "") { this.textContent = text; this.disabled = false; this.listeners = {}; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
}

const nodes = {
  "[data-runner-title]": new NodeStub("Execution is waiting for Founder budget authority."),
  "[data-runner-detail]": new NodeStub("Authorized execution is below the system estimate."),
  "[data-runner-state]": new NodeStub("WAITING FOR BUDGET"),
  "[data-runner-stop]": null,
  "[data-runtime-task]": new NodeStub("Establish reliability baseline"),
  "[data-runtime-employee]": new NodeStub("Engineer"),
  "[data-runtime-run]": new NodeStub("None"),
  "[data-runtime-planning-run]": new NodeStub("Planning Run #57 is complete and remains separate from Employee execution."),
  "[data-runtime-cost]": new NodeStub("NT$ 0"),
};
const monitor = {
  dataset: {operationId: "4"},
  hasAttribute(name) { return false; },
  querySelector(selector) { return nodes[selector] || null; },
};
let fetchCount = 0;
let reloadCount = 0;
const payload = {
  runtime: {
    state: "WAITING_FOR_BUDGET",
    gate_kind: "BUDGET_AUTHORIZATION",
    worker_alive: false,
    active_task: {id: 1, title: "Establish reliability baseline", status: "ASSIGNED", employee: "Engineer"},
    execution_run: null,
    latest_run: null,
    planning_run: {id: 57, purpose: "CEO_FOUNDER_REQUEST", status: "SUCCEEDED", employee: "CEO"},
  },
  operation: {
    status: "WAITING_FOR_FOUNDER",
    spent: "0.0000",
    waiting_reason: "Authorized execution is below the system estimate.",
  },
};

global.window = {
  setTimeout,
  location: { reload() { reloadCount += 1; throw new Error("reload must never be called"); } },
};
global.document = {
  querySelectorAll(selector) { return selector === "[data-operation-monitor]" ? [monitor] : []; },
  body: { contains(node) { return node === monitor; } },
};
global.fetch = async () => {
  fetchCount += 1;
  return {ok: true, async json() { return payload; }};
};

require(path.resolve(process.argv[2]));
setTimeout(() => {
  const failures = [];
  if (fetchCount !== 1) failures.push(`expected one stable-state fetch, got ${fetchCount}`);
  if (reloadCount !== 0) failures.push(`unexpected reload count ${reloadCount}`);
  if (nodes["[data-runner-title]"].textContent !== "Execution is waiting for Founder budget authority.") failures.push("wrong title");
  if (nodes["[data-runtime-run]"].textContent !== "None") failures.push("planning Run leaked into Employee Run");
  if (!nodes["[data-runtime-planning-run]"].textContent.includes("#57")) failures.push("planning Run was not rendered separately");
  if (failures.length) {
    console.error(failures.join("\n"));
    process.exit(1);
  }
  console.log("V0.10.8 browser monitor simulation passed: one stable state, one fetch, zero reloads.");
  process.exit(0);
}, 80);
