"use strict";

(() => {
  const DIMENSIONS = {content: "内容", structure: "结构", language: "语言", handwriting: "书写"};
  const SKILLS = {grammar: "语法", spelling: "拼写", writing_structure: "文章结构", content_development: "内容展开", cohesion: "衔接与连贯", vocabulary_precision: "用词准确性", tense: "时态", subject_verb_agreement: "主谓一致", article: "冠词", plural: "名词复数", preposition: "介词", word_order: "语序", pronoun: "代词", punctuation: "标点"};
  const EDITABLE = ["id", "input_id", "category", "filename", "student_name", "submission_date", "feedback_language", "transcription", "prompt_summary", "errors", "focuses", "strengths", "improvements", "rubric", "teacher_scores", "score_reason", "legacy"];
  const clone = (value) => JSON.parse(JSON.stringify(value));
  const payload = (items) => items.map((item) => Object.fromEntries(EDITABLE.filter((key) => key in item).map((key) => [key, item[key]])));
  const score = (values) => values ? Object.values(values).reduce((sum, value) => sum + Number(value || 0), 0).toFixed(1) : "待核对";
  const skill = (value) => SKILLS[value] || String(value || "练习重点").replace(/[_-]+/g, " ");
  function node(tag, className, text) {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (text !== undefined) result.textContent = String(text);
    return result;
  }
  function button(label, action, className = "button button-secondary button-small") {
    const result = node("button", className, label);
    result.type = "button";
    result.addEventListener("click", action);
    return result;
  }
  function safeURL(url) {
    if (typeof url !== "string" || !url.startsWith("/") || url.startsWith("//")) return null;
    try {
      const parsed = new URL(url, location.origin);
      return parsed.origin === location.origin ? parsed.href : null;
    } catch (_) { return null; }
  }
  function link(label, url) {
    const href = safeURL(url);
    if (!href) return null;
    const result = node("a", null, label);
    result.href = href;
    result.target = "_blank";
    result.rel = "noopener";
    return result;
  }
  async function request(url, method = "GET", body) {
    const abort = new AbortController();
    const timeout = setTimeout(() => abort.abort(), 30000);
    try {
      const response = await fetch(url, {method, credentials: "same-origin", cache: "no-store", signal: abort.signal,
        ...(body ? {headers: {"Content-Type": "application/json", "X-LearnMate-Request": "1"}, body: JSON.stringify(body)} : {})});
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        const messages = {401: "登录已失效，请先在新标签页登录，再返回保存。", 403: "操作未获允许，请从本站页面使用校对功能。", 404: "未找到这份草稿。", 409: "服务器上已有更新，当前修改已保留在此页面。请先查看服务器版本，再决定如何合并。", 422: "部分内容不符合要求，请检查分数、日期与必填内容。"};
        const error = new Error(messages[response.status] || "操作未完成，当前修改仍保留在此页面，请重试。");
        error.status = response.status;
        if ([400, 422].includes(response.status) && typeof data?.detail === "string") error.message = data.detail;
        throw error;
      }
      if (!data || !Array.isArray(data.items)) throw new Error("未能读取完整草稿，请重新载入。");
      return data;
    } finally { clearTimeout(timeout); }
  }

  function create({root, onUpdated = () => {}}) {
    let jobId = null;
    let review = null;
    let items = [];
    let selected = 0;
    let dirty = false;
    let busy = false;
    let accepted = false;
    let epoch = 0;
    let reading = 0;
    let timer = null;
    let message = "";
    let messageKind = "general";
    let isError = false;
    let conflict = false;
    let observedJobVersion = null;
    let fieldCounter = 0;
    let refs = {};

    function endpoint(suffix = "") { return `/api/submissions/${encodeURIComponent(jobId)}/review${suffix}`; }
    function announce(text, error = false, kind = "general") {
      message = text; isError = error; messageKind = error ? "error" : kind;
      if (refs.message) {
        refs.message.textContent = text;
        refs.message.hidden = !text;
        refs.message.className = `notice ${error ? "notice-error" : "notice-warning"}`;
      }
    }
    function announceExport(data, pendingText = "内容已定稿，正在生成 PDF。") {
      const completed = {ready: `定稿 v${data.confirmed_revision} 的 PDF 已生成。`, partial: "部分 PDF 未生成，可重试导出；定稿内容已保存。", failed: "PDF 生成失败，定稿内容已保存。可单独重试导出。"};
      announce(completed[data.pdf_status] || pendingText, false, data.pdf_status === "pending" ? "export-pending" : "export-result");
    }
    function changed() {
      dirty = JSON.stringify(payload(items)) !== JSON.stringify(payload(review.items));
      accepted = false;
      refs.accept.checked = false;
      updateActions(); renderReports();
    }
    function updateActions() {
      if (!review) return;
      refs.version.textContent = dirty ? `草稿 v${review.revision} · 未保存` : `${review.state === "confirmed" ? "已定稿" : "草稿"} v${review.revision}`;
      refs.version.dataset.state = dirty || review.state !== "confirmed" ? "draft" : "confirmed";
      refs.saved.textContent = busy ? "正在保存，请稍候…" : dirty ? "有未保存的修改。保存后请核对最新评分建议，再确认定稿。" : review.state === "confirmed" ? "当前内容已定稿。再次修改后，请保存并重新确认。" : "草稿已保存，确认后才能生成正式 PDF。";
      refs.save.disabled = busy || !dirty || conflict;
      refs.save.textContent = busy ? "正在处理…" : "保存草稿";
      refs.confirm.disabled = busy || dirty || !accepted || conflict || review.state === "confirmed";
      refs.accept.disabled = busy || dirty || conflict || review.state === "confirmed";
      refs.retry.disabled = busy || review.pdf_status === "pending";
      refs.retry.hidden = review.state !== "confirmed" || !["failed", "partial"].includes(review.pdf_status);
      refs.reload.hidden = !conflict;
      refs.conflictLink.hidden = !conflict;
      refs.fields.disabled = busy;
      refs.tabs.querySelectorAll("button").forEach((tab) => { tab.disabled = busy; });
      const scores = items[selected]?.teacher_scores || items[selected]?.system_scores;
      if (refs.total) refs.total.textContent = `${score(scores)} / 20`;
      if (refs.scoreHint) refs.scoreHint.textContent = dirty ? "保存后会更新系统建议；教师定分会保留。" : items[selected]?.teacher_scores ? "已使用教师定分，可填写调整原因。" : "当前采用系统建议，可直接调整各维度分数。";
    }
    function field(label, value, update, options = {}) {
      const wrapper = node("label", "review-field");
      const title = node("span", "review-field-label", label);
      const input = node(options.multiline ? "textarea" : "input", "review-input");
      input.id = options.id || `review-${items[selected]?.id || "item"}-${++fieldCounter}`;
      if (options.multiline) input.rows = options.rows || 3;
      else input.type = options.type || "text";
      input.value = value ?? "";
      input.maxLength = options.maxLength || (options.multiline ? 5000 : 500);
      if (options.min !== undefined) input.min = options.min;
      if (options.max !== undefined) input.max = options.max;
      if (options.step !== undefined) input.step = options.step;
      if (options.required) input.required = true;
      if (options.placeholder) input.placeholder = options.placeholder;
      input.addEventListener("input", () => {
        update(input.value);
        changed();
      });
      wrapper.append(title, input);
      if (options.help) wrapper.append(node("span", "review-help", options.help));
      return wrapper;
    }
    function choice(label, value, choices, update) {
      const wrapper = node("label", "review-field");
      const select = node("select", "review-input");
      choices.forEach(([key, title]) => {
        const option = node("option", null, title); option.value = key; select.append(option);
      });
      select.value = String(value ?? "");
      select.addEventListener("change", () => { update(select.value); changed(); });
      wrapper.append(node("span", "review-field-label", label), select);
      return wrapper;
    }
    function section(title, subtitle) {
      const result = node("section", "review-section");
      result.append(node("h4", null, title));
      if (subtitle) result.append(node("p", "review-help", subtitle));
      return result;
    }
    function lines(label, values, update) {
      return field(label, (values || []).join("\n"), (value) => update(value.split("\n").map((line) => line.trim()).filter(Boolean)), {multiline: true, rows: 3, help: "每行一条。"});
    }
    function skillField(label, value, update, options = {}) {
      return field(label, value ? skill(value) : "", (text) => {
        const known = Object.entries(SKILLS).find(([, name]) => name === text.trim());
        update(known ? known[0] : text);
      }, {...options, maxLength: 200});
    }
    function originalView(item) {
      const detail = node("details", "review-original");
      detail.append(node("summary", null, "查看 AI 原稿与初始评分"));
      const source = review.original_items.find((original) => original.id === item.id);
      if (!source) { detail.append(node("p", "review-help", "这份历史结果未保留完整 AI 原稿。")); return detail; }
      const body = node("div", "review-original-body");
      body.append(node("p", null, `学生：${source.student_name || "未识别"} · 日期：${source.submission_date || "未识别"}`));
      if (source.prompt_summary) body.append(node("h5", null, "题目摘要"), node("p", null, source.prompt_summary));
      if (source.transcription) body.append(node("h5", null, "识别原文"), node("p", null, source.transcription));
      if (source.system_scores) body.append(node("h5", null, `初始评分 ${score(source.system_scores)} / 20`), node("p", null, Object.entries(DIMENSIONS).map(([key, name]) => `${name} ${source.system_scores[key]}`).join(" · ")));
      if (source.errors?.length) {
        body.append(node("h5", null, "初始错题"));
        source.errors.forEach((error) => body.append(node("p", null, `${error.original_answer} → ${error.correct_answer}\n${error.explanation}`)));
      }
      for (const [key, title] of [["strengths", "做得好的地方"], ["improvements", "改进建议"]]) {
        if (source[key]?.length) body.append(node("h5", null, title), node("p", null, source[key].join("\n")));
      }
      if (source.focuses?.length) {
        body.append(node("h5", null, "初始练习重点"));
        source.focuses.forEach((focus) => body.append(node("p", null, `${skill(focus.skill_tag)}：${focus.evidence}\n${focus.suggested_fix}\n${focus.explanation}`)));
      }
      detail.append(body);
      return detail;
    }
    function renderErrors(item, target) {
      const errors = section("错题与纠正", "可删除误判、补充漏判；原答案保留学生实际写的内容。关联练习重点会随错题同步更新。");
      if (!item.errors.length) errors.append(node("p", "review-empty", "当前没有错题，请对照原图核对。"));
      item.errors.forEach((error, index) => {
        const card = node("div", "review-edit-card");
        const header = node("div", "review-card-heading");
        header.append(node("h5", null, `第 ${index + 1} 项`), button("删除这项", () => {
          item.errors.splice(index, 1); changed(); renderEditor();
        }, "text-button review-remove"));
        const pair = node("div", "review-pair");
        pair.append(field("学生原答案 / 错误片段", error.original_answer, (value) => { error.original_answer = value; }, {multiline: true, rows: 2, id: `error-${error.id}-original`}),
          field("建议答案", error.correct_answer, (value) => { error.correct_answer = value; }, {multiline: true, rows: 2}));
        card.append(header, pair, field("解释", error.explanation, (value) => { error.explanation = value; }, {multiline: true, rows: 2}));
        const tags = node("div", "review-pair");
        if (item.category === "writing") tags.append(choice("错误类型", error.kind, [["grammar", "语法"], ["spelling", "拼写"]], (value) => { error.kind = value; error.skill_tag = value; }));
        else tags.append(skillField("知识点", error.skill_tag, (value) => { error.skill_tag = value; }));
        card.append(tags); errors.append(card);
      });
      errors.append(button("＋ 补充错题", () => {
        const id = crypto.randomUUID();
        item.errors.push({id, kind: item.category === "writing" ? "grammar" : "grammar_training", skill_tag: "grammar", original_answer: "", correct_answer: "", explanation: ""});
        changed(); renderEditor(); document.getElementById(`error-${id}-original`)?.focus();
      }));
      target.append(errors);
    }
    function renderScores(item, target) {
      const scores = section("评分", "各维度满分 5 分，总分由四项相加。教师定分不会被系统建议覆盖。");
      if (item.legacy) scores.append(node("p", "notice notice-warning", "历史结果未保留完整评分依据，无法重新计算原评分。你可以直接调整教师分数。"));
      const suggestion = item.system_scores;
      const summary = node("div", "review-score-summary");
      refs.total = node("strong", "review-total");
      summary.append(node("span", null, "当前总分"), refs.total);
      scores.append(summary);
      const grid = node("div", "review-score-grid");
      for (const [key, label] of Object.entries(DIMENSIONS)) {
        grid.append(field(label, (item.teacher_scores || suggestion)?.[key] ?? "", (value) => {
          if (!item.teacher_scores) item.teacher_scores = {...(suggestion || {content: 0, structure: 0, language: 0, handwriting: 0})};
          item.teacher_scores[key] = value === "" ? null : Number(value);
        }, {type: "number", min: 0, max: 5, step: 0.5, required: true, help: `系统建议 ${suggestion?.[key] ?? "未知"}`}));
      }
      refs.scoreHint = node("p", "review-help");
      scores.append(grid, refs.scoreHint);
      if (item.teacher_scores && suggestion) scores.append(button("恢复使用系统建议", () => { item.teacher_scores = null; changed(); renderEditor(); }, "text-button"));
      scores.append(field("调整原因（选填）", item.score_reason, (value) => { item.score_reason = value; }, {multiline: true, rows: 2}));
      if (item.rubric) {
        const detail = node("details", "review-rubric");
        detail.append(node("summary", null, "核对评分依据"));
        const grid = node("div", "review-pair");
        const rubric = item.rubric;
        for (const [key, label] of [["required_points_covered", "已覆盖要点"], ["required_points_total", "题目要求要点数"]]) grid.append(field(label, rubric[key], (value) => { rubric[key] = value === "" ? null : Number(value); }, {type: "number", min: 0, max: 1000, step: 1, required: true}));
        for (const [key, label] of [["has_clear_structure", "结构是否清楚"], ["has_conclusion", "是否有结尾"]]) grid.append(choice(label, rubric[key], [["true", "是"], ["false", "否"]], (value) => { rubric[key] = value === "true"; }));
        grid.append(choice("书写清晰度", rubric.handwriting_legibility, [["excellent", "非常清楚"], ["clear", "清楚"], ["readable", "可辨认"], ["hard_to_read", "较难辨认"], ["illegible", "无法辨认"]], (value) => { rubric.handwriting_legibility = value; }));
        detail.append(node("p", "review-help", "更改依据后，保存草稿会重新计算系统建议。"), grid); scores.append(detail);
      }
      target.append(scores);
    }
    function renderFocuses(item, target) {
      const focuses = section("补充练习重点", "错题对应的练习建议来自上方的纠正与解释；这里补充其他需要练习的能力。");
      item.focuses.forEach((focus, index) => {
        const card = node("div", "review-edit-card");
        const heading = node("div", "review-card-heading");
        heading.append(node("h5", null, `重点 ${index + 1}`), button("删除这项", () => { item.focuses.splice(index, 1); changed(); renderEditor(); }, "text-button review-remove"));
        card.append(heading,
          skillField("练习能力", focus.skill_tag, (value) => { focus.skill_tag = value; }, {id: `focus-${focus.id}-skill`}),
          field("作业中的依据", focus.evidence, (value) => { focus.evidence = value; }, {multiline: true, rows: 2}),
          field("建议", focus.suggested_fix, (value) => { focus.suggested_fix = value; }, {multiline: true, rows: 2}),
          field("解释", focus.explanation, (value) => { focus.explanation = value; }, {multiline: true, rows: 2}));
        focuses.append(card);
      });
      if (!item.focuses.length) focuses.append(node("p", "review-empty", "暂无补充练习重点。"));
      focuses.append(button("＋ 补充练习重点", () => {
        const id = crypto.randomUUID(); item.focuses.push({id, skill_tag: "", evidence: "", suggested_fix: "", explanation: ""});
        changed(); renderEditor(); document.getElementById(`focus-${id}-skill`)?.focus();
      }));
      target.append(focuses);
    }
    function renderEditor() {
      const item = items[selected];
      refs.fields.replaceChildren(node("legend", "visually-hidden", "编辑本份作业")); refs.image.replaceChildren(); refs.total = null; refs.scoreHint = null;
      if (!item) return;
      refs.tabs.querySelectorAll("button").forEach((tab, index) => {
        tab.setAttribute("aria-pressed", String(index === selected));
        tab.className = `review-tab${index === selected ? " is-selected" : ""}`;
      });
      const imageHead = node("div", "review-image-heading");
      imageHead.append(node("h4", null, "原始作业"));
      const original = link("放大原图 ↗", item.image_url);
      if (original) imageHead.append(original);
      refs.image.append(imageHead, node("p", "review-help", item.filename));
      if (original) {
        const img = node("img", "review-source-image"); img.src = original.href; img.alt = `作业原图：${item.filename}`;
        const imageLink = link("", item.image_url); imageLink.setAttribute("aria-label", "在新标签页放大作业原图"); imageLink.append(img);
        img.addEventListener("error", () => { imageLink.replaceWith(node("p", "notice notice-warning", "浏览器无法预览这张图片。可使用上方原图链接打开查看。")); }, {once: true});
        refs.image.append(imageLink);
      } else refs.image.append(node("p", "notice notice-warning", "这份历史结果没有可用的原图关联，请结合你保留的原作业核对。"));
      refs.image.append(originalView(item));
      const details = section("作业信息");
      const pair = node("div", "review-pair");
      pair.append(field("学生姓名", item.student_name, (value) => { item.student_name = value; }, {required: true, maxLength: 200}), field("作业日期", item.submission_date, (value) => { item.submission_date = value; }, {type: "date"}));
      details.append(pair);
      if (item.category === "writing") {
        details.append(field("题目摘要", item.prompt_summary, (value) => { item.prompt_summary = value; }, {multiline: true, rows: 2}),
          field("识别原文", item.transcription, (value) => { item.transcription = value; }, {multiline: true, rows: 8, maxLength: 50000, help: "忠实还原学生写下的内容。修正识别后，请重新核对错题、评分与评语。"}));
      }
      refs.fields.append(details);
      renderErrors(item, refs.fields);
      if (item.category === "writing") {
        renderScores(item, refs.fields);
        const comments = section("给学生的反馈");
        comments.append(lines("做得好的地方", item.strengths, (value) => { item.strengths = value; }), lines("下一步改进", item.improvements, (value) => { item.improvements = value; }));
        refs.fields.append(comments);
      }
      renderFocuses(item, refs.fields);
      updateActions();
    }
    function renderReports() {
      refs.reports.replaceChildren();
      const pending = review.pdf_status === "pending" || (review.history || []).some((entry) => entry.pdf_status === "pending");
      const currentConfirmed = review.state === "confirmed" && !dirty;
      const statusText = currentConfirmed ? ({pending: "内容已定稿，正在生成 PDF。", ready: `定稿 v${review.confirmed_revision} 的 PDF 已生成。`, failed: "定稿内容已保存，PDF 生成失败。可单独重试导出。", partial: "部分 PDF 未生成，可重试导出；定稿内容已保存。"}[review.pdf_status] || "定稿内容已保存。") : review.confirmed_revision ? `当前有${dirty ? "未保存的修改" : "新草稿"}。下方历史定稿 v${review.confirmed_revision} 不包含尚未定稿的修改。` : "保存并确认草稿后，将生成正式 PDF。";
      refs.reports.append(node("p", "review-export-status", statusText));
      function reportsFor(target, reports) {
        (reports || []).forEach((report) => {
          const row = node("div", "review-report-row");
          row.append(node("span", null, report.title || report.filename || "作业报告"));
          const links = node("div", "review-report-actions");
          for (const [label, url] of [["预览 / 打印", report.url], ["下载 PDF", report.download_url]]) {
            const entry = link(label, url); if (entry) links.append(entry);
          }
          row.append(links); target.append(row);
        });
      }
      if (currentConfirmed) reportsFor(refs.reports, review.reports);
      const older = (review.history || []).filter((entry) => entry.revision !== review.revision || !currentConfirmed);
      if (older.length) {
        const history = node("details", "review-original");
        history.append(node("summary", null, `历史定稿（${older.length} 个版本）`));
        older.slice().reverse().forEach((entry) => {
          const group = node("div", "review-history-group");
          const date = new Date(entry.confirmed_at);
          group.append(node("p", "review-help", `定稿 v${entry.revision}${Number.isNaN(date.getTime()) ? "" : ` · ${date.toLocaleString("zh-CN")}`}`));
          reportsFor(group, entry.reports); history.append(group);
        });
        refs.reports.append(history);
      }
      if (pending) schedule();
    }
    function render() {
      root.replaceChildren();
      const header = node("div", "review-heading");
      header.append(node("h3", null, "教师校对")); refs.version = node("span", "review-version"); header.append(refs.version);
      refs.message = node("div", "notice notice-warning"); refs.message.setAttribute("role", "alert");
      const warnings = node("div", "review-warnings");
      if (review.needs_review) warnings.append(node("p", "notice notice-warning", review.revision === 1 ? "AI 草稿尚未经过教师确认，请对照原图核对识别、错题、评分与反馈。" : "当前草稿尚待确认，请核对识别、错题、评分、评语和练习重点。"));
      if (review.failed_files?.length) warnings.append(node("p", "notice notice-warning", `本批有 ${review.failed_files.length} 张图片未成功处理，定稿仅包含下列成功作业。请核对是否需要另行补交。`));
      refs.tabs = node("nav", "review-tabs"); refs.tabs.setAttribute("aria-label", "选择需要校对的作业");
      items.forEach((item, index) => {
        const tab = button(`${index + 1}. ${item.student_name || "待核对学生"} · ${item.category === "writing" ? "作文" : "语法"}`, () => { selected = index; renderEditor(); }, "review-tab");
        refs.tabs.append(tab);
      });
      const layout = node("div", "review-layout"); refs.image = node("aside", "review-image");
      refs.form = node("form", "review-form"); refs.form.addEventListener("submit", (event) => { event.preventDefault(); save(); });
      refs.fields = node("fieldset", "review-fields"); refs.fields.append(node("legend", "visually-hidden", "编辑本份作业")); refs.form.append(refs.fields);
      layout.append(refs.image, refs.form);
      const footer = node("div", "review-footer");
      refs.saved = node("p", "review-save-status"); refs.saved.setAttribute("role", "status"); refs.saved.setAttribute("aria-live", "polite");
      const acceptance = node("label", "review-acceptance"); refs.accept = node("input"); refs.accept.type = "checkbox"; refs.accept.checked = accepted;
      refs.accept.addEventListener("change", () => { accepted = refs.accept.checked; updateActions(); });
      acceptance.append(refs.accept, node("span", null, "我已核对本批全部成功作业的识别、错题、评分与反馈，并了解未处理项目。"));
      const actions = node("div", "review-actions");
      refs.save = button("保存草稿", save, "button button-secondary"); refs.confirm = button("确认并生成 PDF", confirmReview, "button button-primary");
      refs.retry = button("重试 PDF 导出", retryExport, "button button-secondary");
      refs.reload = button("放弃本页修改并载入服务器版本", () => { if (window.confirm("载入服务器版本会替换本页尚未保存的修改。请先在另一个标签页核对或复制需要保留的内容。确定载入吗？")) load(true); }, "text-button review-remove");
      refs.conflictLink = link("在新标签页查看服务器版本 ↗", "/");
      actions.append(refs.save, refs.confirm, refs.retry, refs.conflictLink, refs.reload);
      footer.append(refs.saved, acceptance, actions);
      refs.reports = node("div", "review-reports");
      root.append(header, refs.message, warnings, refs.tabs, layout, footer, refs.reports);
      renderEditor(); renderReports(); announce(message, isError, messageKind);
    }
    function adopt(data) {
      review = data; items = clone(data.items); selected = Math.min(selected, Math.max(0, items.length - 1));
      dirty = false; accepted = false; conflict = false;
      render();
    }
    async function load(force = false) {
      clearTimeout(timer);
      const generation = epoch; const serial = ++reading;
      try {
        const data = await request(endpoint());
        if (generation !== epoch || serial !== reading) return;
        if (dirty && !force) {
          if (data.revision !== review.revision) {
            conflict = true; announce("服务器上已有更新。本页修改仍保留，请先查看服务器版本，再决定如何合并。", true);
          }
          review.pdf_status = data.pdf_status; review.history = data.history; review.reports = data.reports;
          renderReports(); updateActions();
        } else {
          const same = review && data.revision === review.revision;
          if (same && !force) {
            review = data; renderReports(); updateActions();
          } else { announce(""); adopt(data); }
        }
        if (messageKind === "export-pending" && ["ready", "partial", "failed"].includes(data.pdf_status)) announceExport(data);
        if (data.pdf_status === "pending" || (data.history || []).some((entry) => entry.pdf_status === "pending")) schedule();
        else onUpdated();
      } catch (error) {
        if (generation !== epoch || serial !== reading) return;
        announce(error.status ? error.message : "暂时无法读取草稿状态，请检查连接后重试。当前编辑不会被清除。", true);
        if (!review) {
          root.replaceChildren(node("p", "notice notice-error", message), button("重新载入草稿", () => load(true)));
        } else if (review.pdf_status === "pending" || (review.history || []).some((entry) => entry.pdf_status === "pending")) {
          refs.reports.append(button("重新检查 PDF 状态", () => load()));
        }
      }
    }
    function schedule() { clearTimeout(timer); timer = setTimeout(() => load(), 1500); }
    async function mutate(action, body) {
      if (busy) return false;
      busy = true; accepted = false; refs.accept.checked = false;
      clearTimeout(timer); reading += 1; updateActions(); announce("");
      const generation = epoch;
      try {
        const data = await request(endpoint(action), action ? "POST" : "PUT", body);
        if (generation !== epoch) return false;
        adopt(data);
        if (action) announceExport(data, action === "/confirm" ? "已确认定稿，正在准备 PDF。" : "已开始重试 PDF 导出。");
        else announce("草稿已保存，请核对更新后的评分与反馈。", false, "saved");
        onUpdated();
        return true;
      } catch (error) {
        if (generation !== epoch) return false;
        if (error.status === 409) conflict = true;
        announce(error.status ? error.message : "尚未确认操作是否完成。当前修改仍保留，重新保存若提示版本冲突，请先查看服务器版本。", true);
        return false;
      } finally { if (generation === epoch) { busy = false; updateActions(); } }
    }
    async function save() {
      if (!dirty || busy || conflict) return false;
      if (!refs.form.reportValidity()) return false;
      return mutate("", {base_revision: review.revision, items: payload(items)});
    }
    async function confirmReview() {
      if (dirty || busy || conflict || !accepted || review.state === "confirmed") return false;
      return mutate("/confirm", {base_revision: review.revision, reviewed: true});
    }
    async function retryExport() {
      if (busy || review.pdf_status === "pending" || review.state !== "confirmed") return false;
      return mutate("/export", {base_revision: review.confirmed_revision});
    }
    function reset() {
      epoch += 1; reading += 1; clearTimeout(timer);
      jobId = null; review = null; items = []; selected = 0; dirty = false; busy = false; accepted = false; conflict = false;
      observedJobVersion = null; message = ""; messageKind = "general"; refs = {};
      root.replaceChildren(); root.hidden = true; document.body.classList.remove("is-reviewing");
    }
    function open(job) {
      if (!job.review_available) { if (jobId) reset(); return; }
      const jobVersion = `${job.review_revision}:${job.review_state}:${job.pdf_status}`;
      root.hidden = false; document.body.classList.add("is-reviewing");
      if (jobId === job.id) {
        if (jobVersion !== observedJobVersion && !busy) { observedJobVersion = jobVersion; load(); }
        return;
      }
      reset(); jobId = job.id; observedJobVersion = jobVersion;
      root.hidden = false; document.body.classList.add("is-reviewing");
      root.append(node("p", "review-loading", "正在读取校对草稿…")); load();
    }
    function canLeave() {
      if (busy) { announce("正在保存或确认，请等待本次操作完成。", true); return false; }
      return !dirty || window.confirm("本页有未保存的修改。确定离开并放弃这些修改吗？");
    }
    window.addEventListener("beforeunload", (event) => {
      if (dirty || busy) { event.preventDefault(); event.returnValue = ""; }
    });
    return {open, reset, canLeave};
  }
  window.LearnMateReview = {create};
})();
