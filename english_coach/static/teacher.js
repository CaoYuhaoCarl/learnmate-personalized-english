"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const STORAGE_KEY = "learnmate.currentSubmission";
  const MAX_FILE = 10 * 1024 * 1024;
  const MAX_TOTAL = 30 * 1024 * 1024;
  const ALLOWED = /\.(jpe?g|png|webp|heic|heif)$/i;
  const TERMINAL = new Set(["succeeded", "partial", "failed", "interrupted"]);
  const STATUS_LABELS = {queued: "等待处理", running: "正在处理", succeeded: "处理完成", partial: "部分完成", failed: "处理失败", interrupted: "处理已中断"};
  const SKILL_LABELS = {
    writing_structure: "文章结构", content_development: "内容展开", cohesion: "衔接与连贯", vocabulary_precision: "用词准确性",
    grammar: "语法", spelling: "拼写", tense: "时态", subject_verb_agreement: "主谓一致", article: "冠词",
    plural: "名词复数", preposition: "介词", word_order: "语序", pronoun: "代词", punctuation: "标点",
  };
  let selected = [];
  let current = null;
  let pendingId = null;
  let submitting = false;
  let uncertain = false;
  let pollTimer = null;
  let pollErrors = 0;
  let history = [];
  let thumbnailUrls = [];
  let statusRequest = 0;
  let historyRequest = 0;
  let uploadRequestId = null;
  const reviewer = window.LearnMateReview?.create({root: $("teacher-review"), onUpdated: () => { loadHistory(); checkCurrent(); }}) || null;

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function remember(id) {
    pendingId = id;
    try { if (id) localStorage.setItem(STORAGE_KEY, id); else localStorage.removeItem(STORAGE_KEY); } catch (_) { /* Storage may be disabled. */ }
  }
  function busy() { return submitting || uncertain || Boolean(current && !TERMINAL.has(current.status)); }
  function showError(message) { $("form-error").textContent = message; $("form-error").hidden = !message; }
  function formatSize(size) { return size < 1024 * 1024 ? `${Math.max(1, Math.round(size / 1024))} KB` : `${(size / (1024 * 1024)).toFixed(1)} MB`; }
  function fileKey(file) { return `${file.name}\u0000${file.size}\u0000${file.lastModified}`; }
  function skillLabel(tag) { return SKILL_LABELS[tag] || String(tag || "练习重点").replace(/[_-]+/g, " ").trim(); }

  function syncControls() {
    const locked = busy();
    $("files").disabled = locked;
    $("feedback-language").disabled = locked;
    $("clear-files").disabled = locked;
    $("submit-button").disabled = locked || !selected.length;
    $("submit-button").textContent = submitting ? "正在提交…" : "提交作业 →";
    document.querySelectorAll(".remove-file").forEach((button) => { button.disabled = locked; });
    $("new-submission").hidden = !current || !TERMINAL.has(current.status);
    renderHistory();
  }

  function renderSelection() {
    thumbnailUrls.forEach((url) => URL.revokeObjectURL(url));
    thumbnailUrls = [];
    $("file-list").replaceChildren();
    $("selection").hidden = !selected.length;
    $("selection-count").textContent = `已选择 ${selected.length} 张 · ${formatSize(selected.reduce((sum, file) => sum + file.size, 0))}`;
    selected.forEach((file, index) => {
      const row = element("li", "file-row");
      let thumbnail;
      if (/\.(jpe?g|png|webp)$/i.test(file.name)) {
        thumbnail = element("img", "file-thumbnail");
        thumbnail.alt = "";
        const url = URL.createObjectURL(file);
        thumbnailUrls.push(url);
        thumbnail.src = url;
        thumbnail.addEventListener("error", () => { thumbnail.replaceWith(element("span", "file-thumbnail", "图片")); }, {once: true});
      } else thumbnail = element("span", "file-thumbnail", "HEIC");
      const meta = element("div", "file-meta");
      meta.append(element("p", "file-name", file.name), element("p", "file-size", formatSize(file.size)));
      const remove = element("button", "remove-file", "×");
      remove.type = "button";
      remove.setAttribute("aria-label", `移除 ${file.name}`);
      remove.addEventListener("click", () => { selected.splice(index, 1); showError(""); renderSelection(); $("files").focus(); });
      row.append(thumbnail, meta, remove);
      $("file-list").append(row);
    });
    syncControls();
  }

  function addFiles(files) {
    if (busy()) return;
    const next = [...selected];
    for (const file of files) {
      if (!ALLOWED.test(file.name)) { showError("请选择 JPG、PNG、WebP、HEIC 或 HEIF 图片。"); return; }
      if (!file.size) { showError("所选图片为空，请换一张图片。"); return; }
      if (file.size > MAX_FILE) { showError("单张图片不能超过 10 MB，请压缩后重新选择。"); return; }
      if (!next.some((item) => fileKey(item) === fileKey(file))) next.push(file);
    }
    if (next.length > 10) { showError("一次最多提交 10 张图片，请分批处理。"); return; }
    if (next.reduce((sum, file) => sum + file.size, 0) > MAX_TOTAL) { showError("本次图片合计超过 30 MB，请减少图片或压缩后提交。"); return; }
    selected = next;
    showError("");
    renderSelection();
  }

  async function api(url, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), options.method === "POST" ? 60000 : 20000);
    try {
      const response = await fetch(url, {...options, signal: controller.signal, credentials: "same-origin", cache: "no-store"});
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        const messages = {401: "登录已失效，请刷新页面并重新登录。", 403: "本次操作未获允许，请从本站页面重新提交。", 404: "未找到这次提交记录。", 409: "本次提交与已保存的记录不一致，请重新检查状态。", 413: "图片超出大小限制，请减少图片或压缩后提交。", 422: "图片或提交信息不符合要求，请检查后重试。", 429: "当前已有多批作业，请稍后再试。", 503: "服务暂时不可用，请稍后重试或联系管理员。"};
        const error = new Error(messages[response.status] || "请求未完成，请稍后重试。");
        error.status = response.status;
        if ([400, 413, 422].includes(response.status) && typeof data?.detail === "string") error.message = data.detail;
        throw error;
      }
      if (!data || typeof data !== "object") throw new Error("服务返回异常，请重新检查状态。");
      return data;
    } finally { clearTimeout(timeout); }
  }

  function safeLink(url) {
    if (typeof url !== "string" || !url.startsWith("/") || url.startsWith("//")) return null;
    try { const parsed = new URL(url, location.origin); return parsed.origin === location.origin ? parsed.href : null; } catch (_) { return null; }
  }

  function renderReports(reports) {
    $("report-links").replaceChildren();
    for (const report of reports || []) {
      const row = element("div", "report-item");
      row.append(element("p", "report-title", report.title || report.filename || "作业报告"));
      const actions = element("div", "report-actions");
      for (const [label, url, download] of [["预览 / 打印", report.url, false], ["下载 PDF", report.download_url, true]]) {
        const href = safeLink(url);
        if (!href) continue;
        const link = element("a", null, label);
        link.href = href;
        if (!download) { link.target = "_blank"; link.rel = "noopener"; }
        actions.append(link);
      }
      if (actions.childElementCount) { row.append(actions); $("report-links").append(row); }
    }
    $("report-links").hidden = !$("report-links").childElementCount;
  }

  function detail(title, content) {
    const wrapper = element("details", "feedback-detail");
    wrapper.append(element("summary", null, title));
    const body = element("div", "detail-content");
    if (typeof content === "string") body.textContent = content;
    else body.append(content);
    wrapper.append(body);
    return wrapper;
  }

  function renderProfiles(profiles) {
    $("profiles").replaceChildren();
    const dimensionLabels = {content: "内容", structure: "结构", language: "语言", handwriting: "书写"};
    for (const profile of profiles || []) {
      const section = element("section", "profile");
      const header = element("div", "profile-header");
      header.append(element("h3", null, profile.student_name === "unknown" || !profile.student_name ? "待核对学生" : profile.student_name), element("span", null, "本次作业反馈"));
      section.append(header);
      for (const feedback of profile.feedback_items || []) {
        const item = element("article", "feedback-item");
        const heading = element("div", "feedback-heading");
        heading.append(element("h4", null, feedback.filename || "写作反馈"));
        if (typeof feedback.overall_score === "number") {
          const score = element("p", "score", feedback.overall_score.toFixed(1));
          score.append(element("small", null, "/ 20"));
          heading.append(score);
        }
        item.append(heading);
        if (feedback.prompt_summary) item.append(element("p", "feedback-summary", feedback.prompt_summary));
        const dimensions = element("div", "dimension-list");
        Object.entries(feedback.dimensions || {}).forEach(([key, value]) => { dimensions.append(element("span", null, `${dimensionLabels[key] || key} ${value}`)); });
        if (dimensions.childElementCount) item.append(dimensions);
        const columns = element("div", "feedback-columns");
        for (const [title, entries] of [["做得好的地方", feedback.strengths], ["下一步改进", feedback.improvements]]) {
          if (!entries?.length) continue;
          const column = element("div");
          column.append(element("h4", null, title));
          const list = element("ul");
          entries.forEach((entry) => list.append(element("li", null, entry)));
          column.append(list); columns.append(column);
        }
        item.append(columns);
        if (feedback.transcription) item.append(detail("核对识别出的原文", feedback.transcription));
        section.append(item);
      }
      for (const grammar of profile.grammar_trainings || []) {
        const item = element("article", "feedback-item");
        const heading = element("div", "feedback-heading");
        heading.append(element("h4", null, "语法练习反馈")); item.append(heading);
        const mistakes = element("div", "mistakes");
        for (const mistake of grammar.mistakes || []) {
          const row = element("div", "mistake");
          if (mistake.skill_tag) row.append(element("p", "muted small", skillLabel(mistake.skill_tag)));
          for (const [label, value, className] of [["原答案", mistake.original_answer, ""], ["建议答案", mistake.correct_answer, "correction"]]) {
            const line = element("p", className); line.append(element("span", "mistake-label", label), document.createTextNode(value || "未提供")); row.append(line);
          }
          if (mistake.explanation) row.append(element("p", null, mistake.explanation));
          mistakes.append(row);
        }
        if (!mistakes.childElementCount) mistakes.append(element("p", "feedback-summary", "本次未提取到错题，请结合原作业核对。"));
        item.append(mistakes); section.append(item);
      }
      if (profile.learning_needs?.length) {
        const needs = element("div");
        profile.learning_needs.forEach((need) => {
          const block = element("div", "learning-need");
          block.append(element("strong", null, skillLabel(need.skill_tag)));
          if (need.evidence) block.append(element("p", null, `依据：${need.evidence}`));
          if (need.suggested_fix) block.append(element("p", null, `建议：${need.suggested_fix}`));
          if (need.explanation) block.append(element("p", null, need.explanation));
          needs.append(block);
        });
        section.append(detail(`查看 ${profile.learning_needs.length} 项练习重点与依据`, needs));
      }
      (profile.skipped || []).forEach((entry) => section.append(element("p", "profile-skipped", entry)));
      $("profiles").append(section);
    }
  }

  function renderJob(job) {
    current = job;
    remember(job.id);
    $("composer").hidden = true;
    $("empty-result").hidden = true;
    $("status-banner").hidden = false;
    $("status-banner").dataset.status = job.status;
    $("status-title").textContent = job.stage === "reporting" && job.status === "running" ? "反馈已整理，正在生成报告" : (STATUS_LABELS[job.status] || "正在检查状态");
    if (job.review_available) $("status-title").textContent = job.review_state === "confirmed" ? (job.pdf_status === "pending" ? "已定稿，正在生成 PDF" : "教师已确认定稿") : "草稿已生成，等待教师校对";
    $("status-message").textContent = job.message || ({queued: "作业已收到，即将开始处理。", running: "正在识别作业并整理反馈。你可以离开页面，稍后回来查看。", succeeded: "请核对原作业与生成内容后，再交给学生。", partial: "部分图片未能完成，请查看下方说明。", failed: "未能完成本次作业，请查看说明后重新提交。", interrupted: "服务中断，本次处理未完成。请检查已有结果后重新提交。"}[job.status] || "");
    if (job.review_available) $("status-message").textContent = job.review_state === "confirmed" ? "教师确认的内容已保存。再次编辑会形成新草稿，正式报告保留对应版本。" : "请对照原图核对识别、错题与评分，保存并确认后生成正式报告。";
    $("status-icon").textContent = ["running", "queued"].includes(job.status) ? "" : job.status === "succeeded" ? "✓" : "!";
    const hasCount = Number.isInteger(job.completed) && Number.isInteger(job.total) && job.total > 0;
    $("status-count").hidden = !hasCount;
    $("status-count").textContent = hasCount ? `已处理 ${job.completed} / ${job.total} 张` : "";
    $("step-upload").className = "is-done";
    $("step-process").className = TERMINAL.has(job.status) ? "is-done" : "is-current";
    $("step-review").className = TERMINAL.has(job.status) ? "is-current" : "";
    if (job.review_state === "confirmed") $("step-review").className = "is-done";
    const pdfMessage = {failed: "反馈已保留，但 PDF 未能导出。你可以先查看下方反馈；导出工具恢复后再重新处理。", partial: "部分 PDF 未能导出，已生成的报告仍可查看或下载。", not_available: "本次没有可下载的 PDF 报告。"}[job.pdf_status];
    $("pdf-notice").hidden = Boolean(job.review_available) || !pdfMessage || !TERMINAL.has(job.status);
    $("pdf-notice").textContent = pdfMessage || "";
    const outcomes = element("ul");
    (job.files || []).forEach((file) => {
      if (!["succeeded", "failed"].includes(file.status)) return;
      const row = element("li", `outcome-row${file.status === "failed" ? " is-failed" : ""}`);
      row.append(element("span", "outcome-icon", file.status === "succeeded" ? "✓" : "!"), element("span", null, `${file.filename || "图片"}${file.message ? ` · ${file.message}` : ""}`));
      outcomes.append(row);
    });
    $("file-outcomes").replaceChildren(outcomes);
    $("file-outcomes").hidden = !outcomes.childElementCount;
    renderReports(job.review_available ? [] : job.reports);
    $("profiles").hidden = Boolean(job.review_available);
    renderProfiles(job.review_available ? [] : job.profiles);
    reviewer?.open(job);
    syncControls();
  }

  function connectionMessage(message, {retry = false, missing = false} = {}) {
    $("connection-notice").hidden = false;
    $("connection-message").textContent = message;
    $("retry-submit").hidden = !retry;
    $("reset-missing").hidden = !missing;
  }

  async function checkCurrent() {
    clearTimeout(pollTimer);
    const id = pendingId;
    if (!id) return;
    const request = ++statusRequest;
    $("reconnect").disabled = true;
    try {
      const job = await api(`/api/submissions/${encodeURIComponent(id)}`);
      if (pendingId !== id || request !== statusRequest) return;
      uncertain = false; pollErrors = 0;
      uploadRequestId = null;
      $("connection-notice").hidden = true;
      renderJob(job);
      if (!TERMINAL.has(job.status)) pollTimer = setTimeout(checkCurrent, 1500);
      else await loadHistory();
    } catch (error) {
      if (pendingId !== id || request !== statusRequest) return;
      if (error.status === 404) {
        uncertain = true;
        const canRetry = uploadRequestId === id && selected.length > 0;
        connectionMessage(canRetry ? "暂未找到本次提交。可以重新检查状态，或使用同一批图片重试提交。" : "未找到上次提交。记录可能已移除，或上次上传尚未完成，请重新选择图片。", {retry: canRetry, missing: !canRetry});
      } else {
        pollErrors += 1;
        connectionMessage(error.status === 401 ? error.message : "暂时无法读取处理状态。已提交的任务可能仍在运行，请勿重复新建提交。", {});
        if (pollErrors < 4 && error.status !== 401) pollTimer = setTimeout(checkCurrent, Math.min(1500 * (2 ** pollErrors), 12000));
      }
      syncControls();
    } finally { if (request === statusRequest) $("reconnect").disabled = false; }
  }

  async function submit(retry = false) {
    if (submitting || !selected.length || (!retry && busy())) return;
    clearTimeout(pollTimer);
    if (!retry || !pendingId) remember(crypto.randomUUID());
    const id = pendingId;
    const request = ++statusRequest;
    uploadRequestId = id;
    submitting = true; uncertain = false;
    showError(""); $("connection-notice").hidden = true;
    syncControls();
    const form = new FormData();
    form.append("request_id", id);
    form.append("feedback_language", $("feedback-language").value);
    selected.forEach((file) => form.append("files", file, file.name));
    try {
      const job = await api("/api/submissions", {method: "POST", headers: {"X-LearnMate-Request": "1"}, body: form});
      if (pendingId !== id || request !== statusRequest) return;
      uploadRequestId = null;
      renderJob(job);
      pollErrors = 0;
      if (!TERMINAL.has(job.status)) pollTimer = setTimeout(checkCurrent, 1500);
      loadHistory();
    } catch (error) {
      if (pendingId !== id || request !== statusRequest) return;
      if (error.status && [400, 401, 403, 413, 422, 429].includes(error.status)) {
        uploadRequestId = null;
        remember(null);
        showError(error.message);
      } else {
        uncertain = true;
        connectionMessage("尚未确认提交是否完成。正在查询本次记录，请先不要重复提交。");
        pollTimer = setTimeout(checkCurrent, 500);
      }
    } finally { if (request === statusRequest) { submitting = false; syncControls(); } }
  }

  function reset() {
    if (reviewer && !reviewer.canLeave()) return;
    reviewer?.reset();
    clearTimeout(pollTimer);
    statusRequest += 1;
    uploadRequestId = null;
    current = null; uncertain = false; submitting = false; pollErrors = 0;
    remember(null); selected = [];
    $("files").value = "";
    $("composer").hidden = false;
    $("status-banner").hidden = true;
    $("empty-result").hidden = false;
    $("connection-notice").hidden = true;
    $("pdf-notice").hidden = true;
    $("file-outcomes").hidden = true;
    $("report-links").hidden = true;
    $("profiles").replaceChildren();
    $("step-upload").className = "is-current";
    $("step-process").className = "";
    $("step-review").className = "";
    showError(""); renderSelection(); $("files").focus();
  }

  function renderHistory() {
    $("recent-list").replaceChildren();
    $("recent-empty").hidden = Boolean(history.length);
    history.slice(0, 6).forEach((job) => {
      const li = element("li");
      const button = element("button", `recent-button${current?.id === job.id ? " is-selected" : ""}`);
      button.type = "button"; button.disabled = busy();
      const content = element("div", "recent-content");
      const date = new Date(job.created_at);
      const formatted = Number.isNaN(date.getTime()) ? "提交记录" : new Intl.DateTimeFormat("zh-CN", {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"}).format(date);
      const label = job.review_available ? (job.review_state === "confirmed" ? "已定稿" : "待教师校对") : (STATUS_LABELS[job.status] || "等待处理");
      content.append(element("p", "recent-title", formatted), element("p", "recent-detail", `${job.total || 0} 张作业 · ${label}`));
      button.append(element("span", `recent-dot ${job.status || ""}`), content, element("span", "recent-arrow", "↗"));
      button.addEventListener("click", () => {
        if (current?.id === job.id) return;
        if (reviewer && !reviewer.canLeave()) return;
        reviewer?.reset();
        uploadRequestId = null; remember(job.id); uncertain = true; syncControls(); checkCurrent();
      });
      li.append(button); $("recent-list").append(li);
    });
  }

  async function loadHistory() {
    const request = ++historyRequest;
    $("refresh-history").disabled = true;
    try {
      const data = await api("/api/submissions");
      if (request !== historyRequest) return;
      history = Array.isArray(data.submissions) ? data.submissions : [];
      $("recent-error").hidden = true;
      renderHistory();
    } catch (_) {
      if (request !== historyRequest) return;
      $("recent-error").textContent = "暂时无法读取最近提交，可稍后刷新。";
      $("recent-error").hidden = false;
      $("recent-empty").hidden = true;
    } finally { if (request === historyRequest) $("refresh-history").disabled = false; }
  }

  $("files").addEventListener("change", (event) => { addFiles(event.target.files); event.target.value = ""; });
  $("clear-files").addEventListener("click", () => { selected = []; showError(""); renderSelection(); });
  $("upload-form").addEventListener("submit", (event) => { event.preventDefault(); submit(); });
  $("new-submission").addEventListener("click", reset);
  $("reset-missing").addEventListener("click", reset);
  $("reconnect").addEventListener("click", () => { pollErrors = 0; checkCurrent(); });
  $("retry-submit").addEventListener("click", () => submit(true));
  $("refresh-history").addEventListener("click", loadHistory);
  ["dragenter", "dragover"].forEach((name) => $("drop-zone").addEventListener(name, (event) => { event.preventDefault(); if (!busy()) $("drop-zone").classList.add("is-dragging"); }));
  ["dragleave", "drop"].forEach((name) => $("drop-zone").addEventListener(name, (event) => { event.preventDefault(); $("drop-zone").classList.remove("is-dragging"); }));
  $("drop-zone").addEventListener("drop", (event) => addFiles(event.dataTransfer.files));
  window.addEventListener("beforeunload", () => thumbnailUrls.forEach((url) => URL.revokeObjectURL(url)));
  try { pendingId = localStorage.getItem(STORAGE_KEY); } catch (_) { /* Storage may be disabled. */ }
  if (pendingId) {
    uncertain = true;
    $("status-banner").hidden = false;
    $("status-banner").dataset.status = "running";
    $("status-title").textContent = "正在恢复上次提交";
    $("status-message").textContent = "正在读取保存的处理状态。";
    $("empty-result").hidden = true;
    syncControls();
    checkCurrent();
  }
  loadHistory();
})();
