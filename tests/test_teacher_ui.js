"use strict";

// Deterministic interaction-state regressions against the actual browser script.
// This minimal DOM and controlled fetch do not verify layout, accessibility, or
// real browser behavior; those still require the browser acceptance flow.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const script = fs.readFileSync(path.join(__dirname, "../english_coach/static/teacher.js"), "utf8");

class Element {
  constructor() {
    this.children = [];
    this.dataset = {};
    this.hidden = false;
    this.disabled = false;
    this.value = "zh-Hans";
    this.textContent = "";
    this.classList = {add() {}, remove() {}};
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  get childElementCount() { return this.children.length; }
  setAttribute() {}
  addEventListener() {}
  focus() {}
  replaceWith() {}
}

function response(data, status = 200) {
  return {ok: status >= 200 && status < 300, status, json: async () => data};
}

function job(id, status) {
  return {id, status, stage: "finished", completed: 1, total: 1,
    profiles: [], files: [], reports: [], pdf_status: "not_available"};
}

function harness({postStatus = 202} = {}) {
  const elements = new Map();
  const requests = [];
  const get = (id) => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const context = {
    document: {getElementById: get, createElement: () => new Element(),
      createTextNode: (textContent) => ({textContent}), querySelectorAll: () => []},
    window: {addEventListener() {}},
    localStorage: {getItem: () => null, setItem() {}, removeItem() {}},
    location: {origin: "http://localhost"},
    URL, AbortController, Intl, Date, Set, Map,
    FormData: class { append() {} },
    crypto: {randomUUID: () => "synthetic-upload-id"},
    // Polling is driven explicitly by each test, with no elapsed-time dependency.
    setTimeout: () => 1, clearTimeout() {},
    fetch(url, options) {
      if (url === "/api/submissions" && options.method === "POST") {
        return Promise.resolve(response({}, postStatus));
      }
      if (url === "/api/submissions") return Promise.resolve(response({submissions: []}));
      return new Promise((resolve) => requests.push({url, resolve}));
    },
  };
  // Expose the existing closure only within this test VM. Production code stays
  // unchanged; fetch responses exercise its real request and rendering branches.
  const injected = script.replace(/\}\)\(\);\s*$/, `
    globalThis.hooks = {
      checkCurrent, submit, reset, skillLabel,
      setPending(id) { remember(id); uncertain = true; },
      setFiles(files) { selected = files; },
      state() { return {current, pendingId, submitting, uncertain, busy: busy()}; }
    };
  })();`);
  assert.notEqual(injected, script, "The browser script's closure must be located before testing it.");
  vm.createContext(context);
  vm.runInContext(injected, context, {filename: "teacher.js"});
  return {hooks: context.hooks, get, requests};
}

test("skill labels use known teaching terms and preserve unknown meanings", () => {
  const {hooks} = harness();
  assert.equal(hooks.skillLabel("subject_verb_agreement"), "主谓一致");
  assert.equal(hooks.skillLabel("writing_structure"), "文章结构");
  assert.equal(hooks.skillLabel("content_development"), "内容展开");
  assert.equal(hooks.skillLabel("cohesion"), "衔接与连贯");
  assert.equal(hooks.skillLabel("vocabulary_precision"), "用词准确性");
  assert.equal(hooks.skillLabel("unlisted_skill-name"), "unlisted skill name");
});

test("429 rejects the upload clearly and unlocks it without polling a missing job", async () => {
  const {hooks, get, requests} = harness({postStatus: 429});
  hooks.setFiles([{name: "synthetic.png", size: 8, lastModified: 0}]);
  await hooks.submit();
  assert.equal(hooks.state().busy, false);
  assert.equal(hooks.state().pendingId, null);
  assert.equal(get("submit-button").disabled, false);
  assert.match(get("form-error").textContent, /已有多批作业/);
  assert.equal(requests.length, 0);
});

test("a late GET for the same job cannot overwrite a newer completed result", async () => {
  const {hooks, get, requests} = harness();
  hooks.setPending("same-job");
  const older = hooks.checkCurrent();
  const newer = hooks.checkCurrent();
  const oldRequest = requests.shift();
  const newRequest = requests.shift();
  newRequest.resolve(response(job("same-job", "succeeded")));
  await newer;
  oldRequest.resolve(response(job("same-job", "running")));
  await older;
  assert.equal(hooks.state().current.status, "succeeded");
  assert.equal(get("status-title").textContent, "处理完成");
});

test("reset keeps the fresh upload form when an earlier GET finishes", async () => {
  const {hooks, get, requests} = harness();
  hooks.setPending("reset-job");
  const pending = hooks.checkCurrent();
  const request = requests.shift();
  hooks.reset();
  request.resolve(response(job("reset-job", "succeeded")));
  await pending;
  assert.equal(hooks.state().current, null);
  assert.equal(hooks.state().pendingId, null);
  assert.equal(get("composer").hidden, false);
  assert.equal(get("status-banner").hidden, true);
});

test("switching history ignores an earlier error and preserves the new request state", async () => {
  const {hooks, get, requests} = harness();
  hooks.setPending("previous-job");
  const previous = hooks.checkCurrent();
  const previousRequest = requests.shift();
  hooks.setPending("chosen-job");
  const chosen = hooks.checkCurrent();
  const chosenRequest = requests.shift();
  previousRequest.resolve(response({}, 404));
  await previous;
  assert.equal(get("reconnect").disabled, true, "An old finally must not unlock the active request.");
  chosenRequest.resolve(response(job("chosen-job", "succeeded")));
  await chosen;
  assert.equal(hooks.state().current.id, "chosen-job");
  assert.equal(hooks.state().uncertain, false);
  assert.equal(get("connection-notice").hidden, true);
});
