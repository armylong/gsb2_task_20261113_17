/* 本地网页采集工具前端（原生 JS，无外部依赖） */
"use strict";

const state = {
  view: "tasks",
  testSiteBase: null,
  tasks: [],
  detail: null,            // 当前任务详情
  selectedRound: null,     // 当前查看的轮次 id
  recordsPage: 1,
  recordPages: {},         // roundId -> page
  scenario: null,
  testRecords: [],
};

const SCENARIO_DESC = {
  normal: "正常翻页：每页 3 条，next 指向下一页，末页 next 为 null。",
  loop: "记录重复且翻页绕回：第 3 页的 next 指回第 2 页，且 l2 重复出现且内容变化，采集器必须自动停下。",
  ratelimit: "先返回 HTTP 429（带 Retry-After: 1 秒），两次后恢复正常，验证按网站允许时间重试。",
  error: "一直返回 HTTP 500，超过重试次数后该轮失败并记录原因。",
  redirect: "页面 302 自动跳转到另一个端口的“外站”，采集器必须拦截并停止。",
  offsite_next: "首页正常返回，但 next 指向外站地址，采集器必须拦截并停止。",
};

const STATUS_LABEL = {
  queued: "排队", running: "运行中", paused: "已暂停",
  completed: "已完成", failed: "失败",
};

// ---------------- 基础工具 ----------------
async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const text = await resp.text();
  const data = text ? JSON.parse(text) : {};
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

function toast(msg, isError = false) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.style.background = isError ? "#991b1b" : "#111827";
  el.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 3200);
}

function badge(status) {
  return `<span class="badge ${status}">${STATUS_LABEL[status] || status}</span>`;
}

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function showView(name) {
  state.view = name;
  document.querySelectorAll(".view").forEach(v => v.classList.add("hidden"));
  document.getElementById(`view-${name}`).classList.remove("hidden");
  document.querySelectorAll(".nav-btn").forEach(b =>
    b.classList.toggle("active", b.dataset.view === name));
}

// ---------------- 字段配置 ----------------
function fieldRow(path = "", column = "") {
  const row = document.createElement("div");
  row.className = "field-row";
  row.innerHTML = `
    <input placeholder="记录里的字段路径，如 id / author.name" value="${esc(path)}" class="f-path">
    <input placeholder="结果里的列名，如 编号 / 作者姓名" value="${esc(column)}" class="f-column">
    <button type="button" class="btn small danger">删除</button>`;
  row.querySelector("button").onclick = () => row.remove();
  return row;
}

function collectFields() {
  return [...document.querySelectorAll("#field-rows .field-row")]
    .map(r => ({
      path: r.querySelector(".f-path").value.trim(),
      column: r.querySelector(".f-column").value.trim(),
    }))
    .filter(f => f.path);
}

// ---------------- 任务列表 ----------------
async function loadTasks() {
  state.tasks = await api("/api/tasks");
  renderTasks();
}

function renderTasks() {
  const tbody = document.getElementById("task-tbody");
  if (!state.tasks.length) {
    tbody.innerHTML = `<tr><td colspan="8" class="muted">还没有任务，先在上方新建一个。</td></tr>`;
    return;
  }
  tbody.innerHTML = state.tasks.map(t => `
    <tr>
      <td>${t.id}</td>
      <td>${esc(t.name)}<br><code>${esc(t.entry_url)}</code></td>
      <td>${badge(t.status)}</td>
      <td>${t.last_round_no || 0} 轮</td>
      <td>${t.last_pages ?? "—"}</td>
      <td>${t.last_records ?? "—"}</td>
      <td>${t.last_retries ?? "—"}</td>
      <td><button class="btn small" onclick="openDetail(${t.id})">详情</button></td>
    </tr>`).join("");
}

async function createTask(ev) {
  ev.preventDefault();
  const form = ev.target;
  const fields = collectFields();
  if (!fields.length) return toast("至少配置一个字段", true);
  const body = {
    name: form.name.value,
    entry_url: form.entry_url.value.trim(),
    max_concurrency: Number(form.max_concurrency.value),
    min_interval: Number(form.min_interval.value),
    max_retries: Number(form.max_retries.value),
    fields,
  };
  try {
    const task = await api("/api/tasks", { method: "POST", body });
    await api(`/api/tasks/${task.id}/rounds`, { method: "POST" });
    toast("任务已创建并开始第一轮采集");
    form.reset();
    document.getElementById("field-rows").innerHTML = "";
    initDefaultFields();
    await loadTasks();
  } catch (e) {
    toast(e.message, true);
  }
}

// ---------------- 任务详情 ----------------
async function openDetail(taskId) {
  showView("detail");
  await refreshDetail(taskId);
}

async function refreshDetail(taskId) {
  state.detail = await api(`/api/tasks/${taskId}`);
  const t = state.detail;
  document.getElementById("detail-head").innerHTML = `
    <h2>${esc(t.name)} ${badge(t.status)}</h2>
    <p class="muted">入口：<code>${esc(t.entry_url)}</code> ｜ 允许来源：<code>${esc(t.origin)}</code></p>
    <p class="muted">并发上限：${t.max_concurrency} ｜ 同站最小间隔：${t.min_interval}s ｜
       最大重试：${t.max_retries} 次</p>
    <p class="muted">字段：${t.fields.map(f => `${esc(f.path)}→${esc(f.column || f.path)}`).join("，")}</p>
    <div class="row-actions">
      <button class="btn primary" id="new-round-btn"
        ${["running", "paused", "queued"].includes(t.status) ? "disabled" : ""}>
        再跑一轮（更新数据）</button>
    </div>`;
  document.getElementById("new-round-btn").onclick = () => startNewRound(t.id);
  renderRounds();
}

async function startNewRound(taskId) {
  try {
    await api(`/api/tasks/${taskId}/rounds`, { method: "POST" });
    toast("新一轮已开始；完成前上一轮完整结果仍可查看");
    await refreshDetail(taskId);
  } catch (e) {
    toast(e.message, true);
  }
}

function renderRounds() {
  const tbody = document.getElementById("round-tbody");
  const rounds = state.detail.rounds;
  tbody.innerHTML = rounds.map(r => {
    const active = ["queued", "running", "paused"].includes(r.status);
    const ctl = active
      ? (r.status === "paused"
        ? `<button class="btn small" onclick="controlRound(${r.id},'resume')">继续</button>`
        : `<button class="btn small" onclick="controlRound(${r.id},'pause')">暂停</button>`)
      : "";
    const view = `<button class="btn small" onclick="selectRound(${r.id})">查看</button>`;
    const exp = r.status === "completed"
      ? `<a class="btn small" href="/api/rounds/${r.id}/export?format=csv">CSV</a>
         <a class="btn small" href="/api/rounds/${r.id}/export?format=json">JSON</a>` : "";
    return `<tr>
      <td>第 ${r.round_no} 轮</td>
      <td>${badge(r.status)}</td>
      <td>${r.pages_fetched}</td>
      <td>${r.record_count}</td>
      <td>${r.retry_count}</td>
      <td>${r.pending_url ? `<code>${esc(r.pending_url)}</code>` : "—"}</td>
      <td>${r.error ? esc(r.error) : "—"}</td>
      <td>${ctl} ${view} ${exp}</td>
    </tr>`;
  }).join("");
  if (state.selectedRound && rounds.some(r => r.id === state.selectedRound)) {
    renderRoundPanels();
  }
}

async function controlRound(roundId, action) {
  try {
    await api(`/api/rounds/${roundId}/${action}`, { method: "POST" });
    toast(action === "pause" ? "已暂停：不再发新请求，已发出的请求处理完即停" : "已继续");
    await refreshDetail(state.detail.id);
  } catch (e) {
    toast(e.message, true);
  }
}

async function selectRound(roundId) {
  state.selectedRound = roundId;
  state.recordPages[roundId] = state.recordPages[roundId] || 1;
  state.recordsPage = state.recordPages[roundId];
  await renderRoundPanels();
}

async function renderRoundPanels() {
  const round = state.detail.rounds.find(r => r.id === state.selectedRound);
  if (!round) return;
  document.getElementById("records-panel").classList.remove("hidden");
  document.getElementById("failures-panel").classList.remove("hidden");
  document.getElementById("records-title").textContent =
    `第 ${round.round_no} 轮结果记录（${round.status === "completed" ? "已完成快照" : "进行中，数据实时增长"}）`;

  // 对比信息（只有完整轮次才有）
  const diffBox = document.getElementById("diff-box");
  if (round.status === "completed") {
    try {
      const diff = await api(`/api/rounds/${round.id}/diff`);
      if (diff.base_round_no == null) {
        diffBox.innerHTML = `<span class="muted">首轮采集，无历史轮次可对比。</span>`;
      } else {
        diffBox.innerHTML = `<div class="diff-box">
          <span class="diff-chip added">新增 ${diff.summary.added}</span>
          <span class="diff-chip changed">内容变化 ${diff.summary.changed}</span>
          <span class="diff-chip removed">已消失 ${diff.summary.removed}</span>
          <span class="diff-chip unchanged">未变化 ${diff.summary.unchanged}</span>
          <span class="muted">（对比第 ${diff.base_round_no} 轮，按记录 id 判断）</span>
        </div>`;
      }
    } catch (e) {
      diffBox.innerHTML = `<span class="muted">${esc(e.message)}</span>`;
    }
  } else {
    diffBox.innerHTML = `<span class="muted">该轮未完整采完，暂不判断记录消失，避免把没采到的记录误算成删除。</span>`;
  }

  await loadRecords(round.id);
  await loadFailures(round.id);
}

async function loadRecords(roundId) {
  const page = state.recordPages[roundId] || 1;
  const data = await api(`/api/rounds/${roundId}/records?page=${page}&size=10`);
  const table = document.getElementById("records-table");
  const fields = state.detail.fields;
  const cols = fields.map(f => f.column || f.path);
  table.innerHTML = `
    <thead><tr><th>#</th>${cols.map(c => `<th>${esc(c)}</th>`).join("")}<th>历史</th></tr></thead>
    <tbody>${data.records.map((r, i) => `
      <tr>
        <td>${(data.page - 1) * data.size + i + 1}</td>
        ${cols.map(c => `<td>${esc(r.row[c])}</td>`).join("")}
        <td><button class="btn small" onclick="showHistory('${esc(r.record_id)}')">变化</button></td>
      </tr>`).join("") ||
      `<tr><td colspan="${cols.length + 2}" class="muted">暂无记录</td></tr>`}
    </tbody>`;

  const pager = document.getElementById("pager");
  pager.innerHTML = `
    <span class="muted">共 ${data.total} 条，第 ${data.page}/${Math.max(1, data.pages)} 页</span>
    <button class="btn small" ${data.page <= 1 ? "disabled" : ""}
      onclick="gotoPage(${data.page - 1})">上一页</button>
    <button class="btn small" ${data.page >= data.pages ? "disabled" : ""}
      onclick="gotoPage(${data.page + 1})">下一页</button>`;
}

async function gotoPage(page) {
  state.recordPages[state.selectedRound] = page;
  await loadRecords(state.selectedRound);
}

async function loadFailures(roundId) {
  const failures = await api(`/api/rounds/${roundId}/failures`);
  document.getElementById("failures-tbody").innerHTML = failures.map(f => `
    <tr>
      <td><code>${esc(f.url)}</code></td>
      <td>${f.status_code ?? "—"}</td>
      <td>${f.attempts}</td>
      <td>${esc(f.reason)}</td>
    </tr>`).join("") || `<tr><td colspan="4" class="muted">该轮没有失败请求。</td></tr>`;
}

async function showHistory(recordId) {
  const round = state.detail.rounds.find(r => r.id === state.selectedRound);
  const history = await api(`/api/rounds/${round.id}/record/${encodeURIComponent(recordId)}`);
  document.getElementById("modal-title").textContent = `记录 ${recordId} 的各轮次内容`;
  document.getElementById("modal-body").innerHTML = history.map(h => `
    <div class="panel" style="box-shadow:none;border:1px solid #e4e7eb">
      <strong>第 ${h.round_no} 轮（${STATUS_LABEL[h.round_status]}）</strong>
      <pre style="white-space:pre-wrap;word-break:break-all;font-size:12px">${
        esc(JSON.stringify(h.data, null, 2))}</pre>
    </div>`).join("") || `<p class="muted">没有历史。</p>`;
  document.getElementById("modal-mask").classList.remove("hidden");
}

// ---------------- 测试网站 ----------------
async function loadTestSite() {
  const [config, records] = await Promise.all([
    api(state.testSiteBase + "/api/config"),
    api(state.testSiteBase + "/api/records"),
  ]);
  state.scenario = config.scenario;
  state.testRecords = records;
  document.getElementById("testsite-info").innerHTML =
    `接口地址：<code>${esc(state.testSiteBase)}/api/items</code> ｜ 当前场景：<b>${esc(config.scenario)}</b>`;
  document.getElementById("scenario-desc").textContent = SCENARIO_DESC[config.scenario] || "";
  document.getElementById("scenario-btns").innerHTML = config.scenarios.map(s => `
    <button class="${s === config.scenario ? "active" : ""}"
      onclick="switchScenario('${s}')">${s}</button>`).join("");
  renderTestRecords();
}

function renderTestRecords() {
  document.getElementById("testdata-tbody").innerHTML = state.testRecords.map((r, i) => `
    <tr>
      <td><input value="${esc(r.id)}" data-i="${i}" data-k="id"></td>
      <td><input value="${esc(r.title ?? "")}" data-i="${i}" data-k="title"></td>
      <td><input value="${esc(r.price ?? "")}" data-i="${i}" data-k="price" style="width:80px"></td>
      <td><input value="${esc(r.author?.name ?? "")}" data-i="${i}" data-k="author.name" style="width:110px"></td>
      <td><input value="${esc(r.author?.city ?? "")}" data-i="${i}" data-k="author.city" style="width:100px"></td>
      <td>
        <button class="btn small" onclick="saveTestRow(${i})">保存</button>
        <button class="btn small danger" onclick="deleteTestRow('${esc(r.id)}')">删除</button>
      </td>
    </tr>`).join("");
}

function setNested(obj, path, value) {
  const parts = path.split(".");
  let cur = obj;
  for (let i = 0; i < parts.length - 1; i++) {
    cur[parts[i]] = cur[parts[i]] || {};
    cur = cur[parts[i]];
  }
  cur[parts[parts.length - 1]] = value;
}

async function saveTestRow(i) {
  const record = { ...state.testRecords[i], author: { ...(state.testRecords[i].author || {}) } };
  document.querySelectorAll(`#testdata-tbody input[data-i="${i}"]`).forEach(inp => {
    let v = inp.value;
    if (inp.dataset.k === "price") v = v === "" ? undefined : Number(v);
    setNested(record, inp.dataset.k, v);
  });
  await api(state.testSiteBase + "/api/records/add", { method: "POST", body: record });
  toast("已保存，刷新页面再采集即可看到变化");
  await loadTestSite();
}

async function deleteTestRow(id) {
  await fetch(`${state.testSiteBase}/api/records/${encodeURIComponent(id)}`, { method: "DELETE" });
  toast("已删除");
  await loadTestSite();
}

async function addTestRow() {
  const id = prompt("新记录的 id（稳定编号，不可重复）：", "p9");
  if (!id) return;
  const title = prompt("title（中文）：", "新增的商品") || "";
  const name = prompt("author.name（作者姓名）：", "新作者") || "";
  const record = { id, title, price: 50, author: { name, city: "杭州" } };
  await api(state.testSiteBase + "/api/records/add", { method: "POST", body: record });
  toast("已新增");
  await loadTestSite();
}

async function resetTestData() {
  await api(state.testSiteBase + "/api/records/reset", { method: "POST" });
  toast("已恢复初始 8 条数据");
  await loadTestSite();
}

async function switchScenario(s) {
  await api(state.testSiteBase + "/api/control", {
    method: "POST", body: { scenario: s },
  });
  toast(`已切换到场景：${s}`);
  await loadTestSite();
}

async function createDemoTask() {
  try {
    const task = await api("/api/tasks", {
      name: "演示任务 " + new Date().toLocaleTimeString(),
      entry_url: state.testSiteBase + "/api/items",
      max_concurrency: 2,
      min_interval: 0.3,
      max_retries: 3,
      fields: [
        { path: "id", column: "编号" },
        { path: "title", column: "标题" },
        { path: "price", column: "价格" },
        { path: "author.name", column: "作者姓名" },
        { path: "author.city", column: "作者城市" },
        { path: "note", column: "备注" },
      ],
    });
    await api(`/api/tasks/${task.id}/rounds`, { method: "POST", body: {} });
    toast("演示任务已创建并开始采集，正在跳转到任务详情…");
    await openDetail(task.id);
  } catch (e) {
    toast(e.message, true);
  }
}

// ---------------- 轮询与初始化 ----------------
function initDefaultFields() {
  const box = document.getElementById("field-rows");
  box.innerHTML = "";
  [["id", "编号"], ["title", "标题"], ["price", "价格"],
   ["author.name", "作者姓名"], ["author.city", "作者城市"]]
    .forEach(([p, c]) => box.appendChild(fieldRow(p, c)));
}

let pollTimer = null;
async function poll() {
  try {
    if (state.view === "tasks") {
      await loadTasks();
    } else if (state.view === "detail" && state.detail) {
      const hadSelection = state.selectedRound;
      await refreshDetail(state.detail.id);
      if (hadSelection) state.selectedRound = hadSelection;
    } else if (state.view === "testsite") {
      await loadTestSite();
    }
  } catch (e) {
    // 轮询失败不打断页面
    console.warn(e);
  }
}

async function init() {
  const runtime = await api("/api/runtime");
  state.testSiteBase = runtime.test_site_base;

  document.querySelectorAll(".nav-btn").forEach(btn =>
    btn.onclick = () => {
      showView(btn.dataset.view);
      if (btn.dataset.view === "testsite") loadTestSite();
    });
  document.getElementById("back-btn").onclick = () => {
    state.selectedRound = null;
    showView("tasks");
  };
  document.getElementById("add-field").onclick = () =>
    document.getElementById("field-rows").appendChild(fieldRow());
  document.getElementById("task-form").onsubmit = createTask;
  document.getElementById("test-add-btn").onclick = addTestRow;
  document.getElementById("test-reset-btn").onclick = resetTestData;
  document.getElementById("demo-task-btn").onclick = createDemoTask;
  document.getElementById("modal-close").onclick = () =>
    document.getElementById("modal-mask").classList.add("hidden");
  document.getElementById("modal-mask").onclick = e => {
    if (e.target.id === "modal-mask") e.target.classList.add("hidden");
  };

  initDefaultFields();
  await loadTasks();
  pollTimer = setInterval(poll, 2000);
}

window.addEventListener("DOMContentLoaded", init);
