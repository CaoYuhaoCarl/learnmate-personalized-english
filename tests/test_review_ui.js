"use strict";

// These tests execute the shipped browser controller with controlled requests.
// Browser layout and native form validation are verified separately in QA.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const script = fs.readFileSync(path.join(__dirname, "../english_coach/static/review.js"), "utf8");
const clone = (value) => JSON.parse(JSON.stringify(value));

class Element {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.listeners = {}; this.dataset = {};
    this.classList = {add() {}, remove() {}}; this.attributes = {};
    this.textContent = ""; this.value = ""; this.hidden = false; this.disabled = false;
  }
  append(...nodes) { nodes.forEach((node) => { node.parent = this; this.children.push(node); }); }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler); }
  setAttribute(name, value) { this.attributes[name] = value; }
  querySelectorAll(selector) { return descendants(this).filter((node) => node.tagName === selector); }
  reportValidity() { return true; }
  focus() {}
  dispatch(name) { return Promise.all((this.listeners[name] || []).map((handler) => handler({target: this, preventDefault() {}}))); }
}
function descendants(root) { return root.children.flatMap((child) => [child, ...descendants(child)]); }
function item() {
  return {id: "input-1", input_id: "input-1", category: "writing", filename: "synthetic.png", student_name: "Demo Learner", submission_date: "2026-11-06", feedback_language: "zh-Hans",
    transcription: "He go to school.", prompt_summary: "Daily routines", errors: [{id: "error-1", kind: "grammar", skill_tag: "grammar", original_answer: "He go", correct_answer: "He goes", explanation: "Check subject and verb."}],
    focuses: [], strengths: ["Clear topic"], improvements: ["Check the verb"], rubric: {required_points_covered: 1, required_points_total: 1, has_clear_structure: true, has_conclusion: true, handwriting_legibility: "clear"},
    teacher_scores: null, score_reason: "", system_scores: {content: 5, structure: 5, language: 4.5, handwriting: 4}, overall_score: 18.5, legacy: false,
    image_url: "/api/submissions/synthetic/inputs/input-1"};
}
function doc() {
  return {revision: 1, state: "draft", items: [item()], original_items: [item()], needs_review: false, legacy: false,
    confirmed_revision: null, confirmed_at: null, pdf_status: "not_available", reports: [], history: [], failed_files: []};
}
function response(data, status = 200) { return {ok: status >= 200 && status < 300, status, json: async () => clone(data)}; }
async function flush() { for (let index = 0; index < 12; index += 1) await Promise.resolve(); }

function harness() {
  const root = new Element("div"); const requests = []; const windowEvents = {}; const timers = new Map(); let timerId = 0;
  const context = {
    document: {body: new Element("body"), createElement: (tag) => new Element(tag),
      getElementById: (id) => descendants(root).find((entry) => entry.id === id)},
    window: {addEventListener(name, handler) { windowEvents[name] = handler; }, confirm: () => false},
    location: {origin: "http://localhost"}, URL, AbortController,
    setTimeout(callback, delay) { timerId += 1; timers.set(timerId, {callback, delay}); return timerId; },
    clearTimeout(id) { timers.delete(id); },
    crypto: {randomUUID: () => "new-error-id"},
    fetch(url, options) { return new Promise((resolve) => requests.push({url, options, resolve})); },
  };
  const injected = script.replace("return {open, reset, canLeave};", "return {open, reset, canLeave, save, confirmReview, retryExport, load, state: () => ({review, items, dirty, busy, accepted, conflict})};");
  assert.notEqual(injected, script);
  vm.createContext(context); vm.runInContext(injected, context, {filename: "review.js"});
  const controller = context.window.LearnMateReview.create({root});
  const button = (label) => descendants(root).find((entry) => entry.tagName === "button" && entry.textContent === label);
  const input = (label) => descendants(root).find((entry) => entry.tagName === "label" && entry.children[0]?.textContent === label)?.children[1];
  const text = () => descendants(root).map((entry) => entry.textContent).join("\n");
  async function begin(data = doc()) {
    controller.open({id: "synthetic", review_available: true, review_revision: 1});
    requests.shift().resolve(response(data)); await flush();
  }
  async function edit(label, value) { const entry = input(label); assert.ok(entry, label); entry.value = value; await entry.dispatch("input"); }
  return {root, requests, controller, button, input, text, begin, edit, windowEvents, timers};
}

test("teacher scores save as overrides while system recommendations remain available", async () => {
  const h = harness(); await h.begin();
  await h.edit("语言", "5");
  assert.equal(h.controller.state().dirty, true);
  assert.equal(h.controller.state().items[0].teacher_scores.language, 5);
  assert.equal(h.controller.state().items[0].system_scores.language, 4.5);
  assert.equal(h.button("确认并生成 PDF").disabled, true);
  const saving = h.controller.save(); const request = h.requests.shift();
  assert.equal(request.options.method, "PUT");
  const body = JSON.parse(request.options.body);
  assert.equal(body.base_revision, 1);
  assert.equal(body.items[0].teacher_scores.language, 5);
  assert.equal("image_url" in body.items[0], false);
  assert.equal("system_scores" in body.items[0], false);
  const saved = doc(); saved.revision = 2; saved.items[0].teacher_scores = body.items[0].teacher_scores; saved.items[0].overall_score = 19;
  request.resolve(response(saved)); await saving;
  assert.equal(h.controller.state().dirty, false);
  assert.equal(h.controller.state().review.revision, 2);
  assert.match(h.text(), /19\.0 \/ 20/);
});

test("a rejected stale save keeps local corrections and blocks accidental confirmation", async () => {
  const h = harness(); await h.begin(); await h.edit("识别原文", "He goes to school.");
  const saving = h.controller.save(); h.requests.shift().resolve(response({}, 409)); await saving;
  assert.equal(h.controller.state().items[0].transcription, "He goes to school.");
  assert.equal(h.controller.state().dirty, true);
  assert.equal(h.controller.state().conflict, true);
  assert.equal(h.button("确认并生成 PDF").disabled, true);
  assert.equal(h.button("放弃本页修改并载入服务器版本").hidden, false);
  assert.match(h.text(), /当前修改已保留/);
});

test("polling export status does not overwrite a teacher's unsaved content", async () => {
  const h = harness(); const initial = doc(); initial.pdf_status = "pending"; await h.begin(initial);
  await h.edit("下一步改进", "Teacher's chosen feedback");
  const polling = h.controller.load(); const completed = doc(); completed.pdf_status = "ready";
  h.requests.shift().resolve(response(completed)); await polling;
  assert.equal(h.controller.state().items[0].improvements[0], "Teacher's chosen feedback");
  assert.equal(h.controller.state().dirty, true);
  assert.equal(h.input("下一步改进").value, "Teacher's chosen feedback");
});

test("a late response after switching submissions cannot restore the old editor", async () => {
  const h = harness(); await h.begin();
  const loading = h.controller.load(); const old = h.requests.shift();
  h.controller.reset();
  old.resolve(response(doc())); await loading;
  assert.equal(h.controller.state().review, null);
  assert.equal(h.root.hidden, true);
  assert.equal(h.root.children.length, 0);
});

test("confirmation requires explicit review and binds the saved version", async () => {
  const h = harness(); await h.begin();
  assert.equal(await h.controller.confirmReview(), false);
  assert.equal(h.requests.length, 0);
  const checkbox = descendants(h.root).find((entry) => entry.type === "checkbox");
  checkbox.checked = true; await checkbox.dispatch("change");
  const confirming = h.controller.confirmReview(); const request = h.requests.shift();
  assert.equal(request.url, "/api/submissions/synthetic/review/confirm");
  assert.deepEqual(JSON.parse(request.options.body), {base_revision: 1, reviewed: true});
  const confirmed = doc(); confirmed.state = "confirmed"; confirmed.confirmed_revision = 1; confirmed.pdf_status = "pending";
  request.resolve(response(confirmed, 202)); await confirming;
  assert.equal(h.controller.state().review.state, "confirmed");
  assert.match(h.text(), /内容已定稿，正在生成 PDF/);
});

test("PDF retry uses the confirmed revision without resubmitting any images", async () => {
  const h = harness(); const initial = doc(); initial.state = "confirmed"; initial.confirmed_revision = 1; initial.pdf_status = "failed";
  await h.begin(initial);
  const retrying = h.controller.retryExport(); const request = h.requests.shift();
  assert.equal(request.url, "/api/submissions/synthetic/review/export");
  assert.deepEqual(JSON.parse(request.options.body), {base_revision: 1});
  initial.pdf_status = "pending"; request.resolve(response(initial, 202)); await retrying;
  assert.equal(h.controller.state().review.items[0].transcription, "He go to school.");
});

test("completed export polling replaces its pending notice with the actual result", async () => {
  for (const [status, expected] of [["ready", /定稿 v1 的 PDF 已生成/], ["partial", /部分 PDF 未生成/], ["failed", /PDF 生成失败/]]) {
    const h = harness(); await h.begin();
    const checkbox = descendants(h.root).find((entry) => entry.type === "checkbox");
    checkbox.checked = true; await checkbox.dispatch("change");
    const confirming = h.controller.confirmReview();
    const confirmed = doc(); confirmed.state = "confirmed"; confirmed.confirmed_revision = 1; confirmed.pdf_status = "pending";
    h.requests.shift().resolve(response(confirmed, 202)); await confirming;
    assert.match(h.text(), /正在准备 PDF/);
    const polling = h.controller.load(); confirmed.pdf_status = status;
    h.requests.shift().resolve(response(confirmed)); await polling;
    assert.doesNotMatch(h.text(), /正在准备 PDF/);
    assert.match(h.text(), expected);
  }
});

test("export completion preserves an intervening save error and the teacher's correction", async () => {
  const h = harness(); const initial = doc(); initial.state = "confirmed"; initial.confirmed_revision = 1; initial.pdf_status = "pending";
  await h.begin(initial); await h.edit("识别原文", "Teacher's correction");
  const saving = h.controller.save(); h.requests.shift().resolve(response({detail: "Please check this correction."}, 422)); await saving;
  const polling = h.controller.load(); initial.pdf_status = "ready";
  h.requests.shift().resolve(response(initial)); await polling;
  assert.match(h.text(), /Please check this correction\./);
  assert.equal(h.controller.state().items[0].transcription, "Teacher's correction");
  assert.equal(h.controller.state().dirty, true);
});

test("adding and deleting mistakes preserves stable IDs and updates the save payload", async () => {
  const h = harness(); await h.begin();
  await h.button("删除这项").dispatch("click");
  assert.equal(h.controller.state().items[0].errors.length, 0);
  await h.button("＋ 补充错题").dispatch("click");
  assert.equal(h.controller.state().items[0].errors[0].id, "new-error-id");
  assert.equal(h.controller.state().items[0].errors[0].kind, "grammar");
  assert.equal(h.controller.canLeave(), false);
});

test("editing a confirmed result identifies its PDF as a previous version immediately", async () => {
  const h = harness(); const initial = doc();
  initial.state = "confirmed"; initial.confirmed_revision = 1; initial.pdf_status = "ready";
  initial.reports = [{filename: "synthetic.pdf", title: "Demo report", url: "/report.pdf", download_url: "/report.pdf?download=1"}];
  initial.history = [{revision: 1, confirmed_at: "2026-11-06T00:00:00Z", pdf_status: "ready", reports: initial.reports}];
  await h.begin(initial); await h.edit("调整原因（选填）", "A teacher revision");
  assert.match(h.text(), /当前有未保存的修改/);
  assert.match(h.text(), /历史定稿（1 个版本）/);
  assert.doesNotMatch(h.text(), /定稿 v1 的 PDF 已生成/);
});

test("a newer draft continues checking an older pending export without replacing edits", async () => {
  const h = harness(); const initial = doc(); initial.revision = 2; initial.confirmed_revision = 1;
  initial.history = [{revision: 1, confirmed_at: "2026-11-06T00:00:00Z", pdf_status: "pending", reports: []}];
  await h.begin(initial);
  assert.ok([...h.timers.values()].some((entry) => entry.delay === 1500));
  await h.edit("识别原文", "The teacher's correction");
  const loading = h.controller.load(); const updated = clone(initial); updated.history[0].pdf_status = "ready";
  h.requests.shift().resolve(response(updated)); await loading;
  assert.equal(h.controller.state().items[0].transcription, "The teacher's correction");
  assert.equal([...h.timers.values()].some((entry) => entry.delay === 1500), false);
});
