"use strict";

// ---- 列表页：run 管理（搜索 / 筛选 / 排序 / 打开 / 删除 / 新建） ----

const $ = (id) => document.getElementById(id);
const searchInput = $("search-input");
const dateFrom = $("date-from");
const dateTo = $("date-to");
const searchClear = $("search-clear");
const queryBtn = $("query-btn");
const clearBtn = $("clear-btn");
const refreshBtn = $("refresh-btn");
const newRunBtn = $("new-run-btn");
const runTbody = $("run-tbody");
const listEmpty = $("list-empty");
const listFooter = $("list-footer");
const listCount = $("list-count");
const pageInfo = $("page-info");
const pagePrev = $("page-prev");
const pageNext = $("page-next");
const statusBar = $("status-bar");
const sortTime = $("sort-time");
const sortArrow = $("sort-arrow");
const newRunModal = $("new-run-modal");
const newRunInput = $("new-run-input");
const modalCancel = $("modal-cancel");
const modalConfirm = $("modal-confirm");

let allRuns = [];       // 后端返回的完整列表
let sortDesc = true;    // 开始时间排序：默认倒序（最新在前）
let currentPage = 1;    // 当前页码（从 1 开始）
let pageSize = 10;      // 每页条数（默认 10，可在分页底栏切换）

const STATUS_LABEL = {
  running: "运行中",
  awaiting_approval: "待审批",
  completed: "已完成",
  incomplete: "未完成",
  failed: "失败",
};

// 结束方式的中文说明，鼠标悬停在状态上时显示
const ENDED_BY_LABEL = {
  finish_tool: "模型主动声明完成",
  text_response: "模型直接给出回答（未调用 finish）",
  iteration_cap: "跑满迭代次数仍未得出结论",
  empty_output: "模型返回空内容",
  error: "运行出错",
};

function shorten(s, n) {
  return s && s.length > n ? s.slice(0, n) + "…" : s;
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function setStatus(msg) { statusBar.textContent = msg; }

// ---- 自绘下拉组件 ----
// 原生 select 的弹出层由系统绘制，option 样式改不动，
// 这里用 div + button 自己实现一套，弹出列表就能跟主题走。
const dropdowns = [];

function closeAllDropdowns(except) {
  for (const d of dropdowns) if (d.root !== except) d.close();
}

// root 结构见 index.html：.dropdown > (.dropdown-toggle, .dropdown-menu)
function createDropdown(root, onChange) {
  const toggle = root.querySelector(".dropdown-toggle");
  const textEl = root.querySelector(".dropdown-text");
  const menu = root.querySelector(".dropdown-menu");

  let options = [];                                  // [{ value, label }]
  let current = root.dataset.value || "";

  function markSelected() {
    for (const el of menu.children) {
      const hit = el.dataset.value === current;
      el.classList.toggle("selected", hit);
      el.setAttribute("aria-selected", String(hit));
    }
    const picked = options.find((o) => o.value === current);
    textEl.textContent = picked ? picked.label : "";
    // 定宽 + 省略号之后，长选项（如 deepseek-v4-flash）会被截断，
    // 把完整文字挂到 title 上，悬浮能看到全名
    toggle.title = textEl.textContent;
    root.classList.toggle("has-value", !!current);
  }

  function close() {
    root.classList.remove("open");
    toggle.setAttribute("aria-expanded", "false");
  }

  function open() {
    closeAllDropdowns(root);
    root.classList.add("open");
    toggle.setAttribute("aria-expanded", "true");
  }

  // notify=false 用于程序化赋值，避免触发查询回调
  function choose(value, notify) {
    current = value;
    markSelected();
    close();
    if (notify && onChange) onChange(value);
  }

  const api = {
    root,
    close,
    get value() { return current; },
    set value(v) { choose(v, false); },
    setOptions(list) {
      options = list;
      menu.innerHTML = "";
      for (const o of list) {
        const el = document.createElement("button");
        el.type = "button";
        el.className = "dropdown-item";
        el.dataset.value = o.value;
        el.textContent = o.label;
        el.setAttribute("role", "option");
        el.addEventListener("click", (e) => {
          e.stopPropagation();
          choose(o.value, true);
        });
        menu.appendChild(el);
      }
      // 当前值已不在新选项里（比如模型下线了）就退回第一项
      if (!list.some((o) => o.value === current)) current = list[0] ? list[0].value : "";
      markSelected();
      return current;
    },
  };

  toggle.addEventListener("click", (e) => {
    e.stopPropagation();
    root.classList.contains("open") ? close() : open();
  });
  // 点击组件外部关闭；Esc 关闭并把焦点还给按钮
  document.addEventListener("click", (e) => { if (!root.contains(e.target)) close(); });
  root.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && root.classList.contains("open")) { close(); toggle.focus(); }
  });

  dropdowns.push(api);
  return api;
}

// ---- 加载列表 ----
async function loadRuns() {
  try {
    // 先拿到系统注册表再渲染，否则首屏会闪一下原始 key（"canvas"）
    await Systems.load();
    const res = await fetch("/api/runs");
    const data = await res.json();
    allRuns = data.runs || [];
    populateModels();
    populateSources();
    render();
    syncSearchClear();
    setStatus(`共 ${allRuns.length} 条运行记录`);
  } catch (e) {
    setStatus("加载失败: " + e.message);
  }
}

// ---- 模型下拉：选项来自全量列表，不受当前筛选影响 ----
function populateModels() {
  const models = [...new Set(allRuns.map((r) => r.model).filter(Boolean))].sort();
  // setOptions 内部会保留当前选择；该模型已不存在则退回“全部模型”
  modelDropdown.setOptions([
    { value: "", label: "全部模型" },
    ...models.map((m) => ({ value: m, label: m })),
  ]);
}

// ---- 系统下拉：注册表里的全部系统 ∪ 数据里实际出现过的 ----
// 只列注册表会漏掉「还没登记就上报了」的新系统；
// 只列数据里出现过的，又会让还没数据的系统在下拉里消失。所以取并集。
function populateSources() {
  const observed = [...new Set(allRuns.map((r) => r.source).filter(Boolean))].sort();
  sourceDropdown.setOptions([
    { value: "", label: "全部系统" },
    ...Systems.options(observed),
  ]);
}

// ---- 过滤 + 排序 + 渲染 ----
function filteredRuns() {
  const kw = searchInput.value.trim().toLowerCase();
  const st = statusDropdown.value;
  const mo = modelDropdown.value;
  const so = sourceDropdown.value;
  const from = dateFrom.value;   // "YYYY-MM-DD" 或 ""
  const to = dateTo.value;

  let list = allRuns.filter((r) => {
    if (st && r.status !== st) return false;
    if (mo && (r.model || "") !== mo) return false;
    // 空 source 的老记录归到看板自建，与后端回填规则保持一致
    if (so && (r.source || "dashboard") !== so) return false;
    if (kw && !(r.task || "").toLowerCase().includes(kw)) return false;
    // started_at 形如 "2026-09-18 13:50:49"，取前 10 位与 date 控件的值直接比大小
    const day = (r.started_at || "").slice(0, 10);
    if (from && (!day || day < from)) return false;
    if (to && (!day || day > to)) return false;
    return true;
  });
  // 按开始时间排序
  list.sort((a, b) => {
    const t = (a.started_at || "").localeCompare(b.started_at || "");
    return sortDesc ? -t : t;
  });
  return list;
}

// 总页数。列表为空时也算 1 页，避免出现「第 1 / 0 页」。
function totalPages(total) {
  return Math.max(1, Math.ceil(total / pageSize));
}

function render() {
  const list = filteredRuns();       // 已过滤 + 已排序的完整结果
  const total = list.length;

  // 先夹住页码再切片：删掉最后一页的唯一一条、或查询后结果变少时，
  // currentPage 会越界，不夹的话会渲染出一个空页
  const pages = totalPages(total);
  if (currentPage > pages) currentPage = pages;
  if (currentPage < 1) currentPage = 1;

  const start = (currentPage - 1) * pageSize;
  const page = list.slice(start, start + pageSize);

  runTbody.innerHTML = "";
  // 行数交给 CSS：列表区有富余高度时用它把行撑满，见 .run-table 的 height
  runTbody.parentElement.style.setProperty("--row-count", page.length);
  listEmpty.style.display = total ? "none" : "";
  listEmpty.textContent = total ? "" : "暂无运行记录";
  renderPager(total, pages, start, page.length);

  for (const r of page) {
    const tr = document.createElement("tr");
    tr.className = "run-row";
    tr.title = "点击打开详情";

    // 结束方式作为状态块的提示文字：状态回答"有没有结果"，
    // ended_by 回答"怎么结束的"——后者才看得出模型是真做完了还是半途停下
    const endedHint = ENDED_BY_LABEL[r.ended_by];

    tr.innerHTML =
      `<td class="col-task" title="${escapeHtml(r.task || "")}">${escapeHtml(shorten(r.task, 60))}</td>` +
      `<td class="col-system">${escapeHtml(Systems.labelOf(r.source))}</td>` +
      `<td class="col-model">${escapeHtml(r.model || "-")}</td>` +
      `<td class="col-status"><span class="run-status ${r.status}"` +
      `${endedHint ? ` title="${escapeHtml(endedHint)}"` : ""}>` +
      `${STATUS_LABEL[r.status] || r.status}</span></td>` +
      `<td class="col-time">${escapeHtml(r.started_at || "-")}</td>` +
      `<td class="col-tokens">${r.input_tokens}/${r.output_tokens}</td>` +
      `<td class="col-actions"></td>`;

    // 操作按钮（阻止冒泡，避免触发行点击）
    const actionsTd = tr.querySelector(".col-actions");

    const openBtn = document.createElement("button");
    openBtn.className = "row-btn";
    openBtn.textContent = "打开";
    openBtn.title = "打开详情";
    openBtn.addEventListener("click", (e) => { e.stopPropagation(); openRun(r.id); });
    actionsTd.appendChild(openBtn);

    const delBtn = document.createElement("button");
    delBtn.className = "row-btn danger";
    delBtn.textContent = "删除";
    delBtn.title = "删除记录";
    delBtn.addEventListener("click", (e) => { e.stopPropagation(); deleteRun(r.id); });
    actionsTd.appendChild(delBtn);

    // 行点击打开详情
    tr.addEventListener("click", () => openRun(r.id));

    runTbody.appendChild(tr);
  }

  return list;   // 调用方需要条数时直接取，避免重复过滤
}

// ---- 分页底栏 ----
function renderPager(total, pages, start, shown) {
  listFooter.classList.toggle("hidden", total === 0);
  if (!total) return;

  // 总数和区间分开写：既知道一共多少条，也知道当前看的是哪一段
  listCount.textContent = shown === total
    ? `共 ${total} 条`
    : `共 ${total} 条 · 当前 ${start + 1}–${start + shown}`;
  pageInfo.textContent = `第 ${currentPage} / ${pages} 页`;
  pagePrev.disabled = currentPage <= 1;
  pageNext.disabled = currentPage >= pages;
}

function gotoPage(n) {
  const pages = totalPages(filteredRuns().length);
  const target = Math.min(Math.max(n, 1), pages);
  if (target === currentPage) return;
  currentPage = target;
  render();
  // 翻页后表格是新的内容，滚回顶部，否则还停在上一页的滚动位置。
  // 滚动容器是 .table-scroll（整页不滚，只有列表区滚），
  // 这是尽力而为的动作，失败了也不能影响翻页本身。
  const scroller = document.querySelector(".table-scroll");
  if (scroller && typeof scroller.scrollTo === "function") {
    scroller.scrollTo({ top: 0, behavior: "smooth" });
  }
}

function openRun(runId) {
  window.location.href = "/run?id=" + encodeURIComponent(runId);
}

// ---- 删除 ----
async function deleteRun(runId) {
  if (!confirm("确定删除这条运行记录吗？删除后不可恢复。")) return;
  try {
    const res = await fetch(`/api/runs/${runId}`, { method: "DELETE" });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      setStatus("删除失败: " + (err.detail || res.status));
      return;
    }
    await loadRuns();
  } catch (e) {
    setStatus("请求出错: " + e.message);
  }
}

// ---- 新建任务（自定义弹框） ----
function openNewRunModal() {
  newRunInput.value = "";
  newRunModal.classList.remove("hidden");
  newRunInput.focus();
}

function closeNewRunModal() {
  newRunModal.classList.add("hidden");
}

async function submitNewRun() {
  const t = newRunInput.value.trim();
  if (!t) { setStatus("任务不能为空"); return; }
  try {
    modalConfirm.disabled = true;
    setStatus("创建中…");
    const res = await fetch("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task: t }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      setStatus("创建失败: " + (err.detail || res.status));
      modalConfirm.disabled = false;
      return;
    }
    const data = await res.json();
    window.location.href = "/run?id=" + encodeURIComponent(data.run_id);
  } catch (e) {
    setStatus("请求出错: " + e.message);
    modalConfirm.disabled = false;
  }
}

// ---- 清空查询条件 ----
// 内嵌 ✕ 只在输入框有内容时显示
function syncSearchClear() {
  searchClear.classList.toggle("hidden", !searchInput.value);
}

function clearQuery() {
  searchInput.value = "";
  statusDropdown.value = "";   // 走 setter，不会触发 onChange
  modelDropdown.value = "";
  sourceDropdown.value = "";
  dateFrom.value = "";
  dateTo.value = "";
  syncSearchClear();
  currentPage = 1;
  render();
  setStatus("已清空查询条件");
}

// 日期区间倒置时结果必然为空，给一句提示
function dateRangeHint() {
  return dateFrom.value && dateTo.value && dateFrom.value > dateTo.value
    ? "（开始时间晚于结束时间）"
    : "";
}

// 条件类控件只负责记录输入，列表要等点「查询」才刷新
function onConditionEdit() {
  syncSearchClear();
  setStatus("条件已修改，点「🔍 查询」刷新列表");
}

// 执行查询：此时才真正用当前条件重渲染列表
function runQuery() {
  // 条件变了，结果集也变了，原来的页码没有意义
  currentPage = 1;
  const list = render();
  setStatus(`查询完成，共 ${list.length} 条` + dateRangeHint());
}

// 仅清空关键词，保留其它条件；同样不自动刷新
function clearKeyword() {
  searchInput.value = "";
  syncSearchClear();
  searchInput.focus();
  setStatus("已清空任务关键词，点「🔍 查询」刷新列表");
}

// ---- 刷新（重新拉取后端列表） ----
async function refreshRuns() {
  setStatus("刷新中…");
  await loadRuns();
}

// ---- 排序切换 ----
function toggleSort() {
  sortDesc = !sortDesc;
  sortArrow.textContent = sortDesc ? "▼" : "▲";
  // 顺序全变了，原来的「第 3 页」已经没有意义，回到第 1 页
  currentPage = 1;
  render();
}

// ---- 三个筛选下拉 ----
// 必须放在 createDropdown 定义和 dropdowns 声明之后，
// 否则会踩 const 的暂时性死区（ReferenceError）
const statusDropdown = createDropdown($("status-filter"), onConditionEdit);
const modelDropdown = createDropdown($("model-filter"), onConditionEdit);
const sourceDropdown = createDropdown($("source-filter"), onConditionEdit);

statusDropdown.setOptions([
  { value: "", label: "全部状态" },
  { value: "running", label: "运行中" },
  { value: "awaiting_approval", label: "待审批" },
  { value: "completed", label: "已完成" },
  { value: "incomplete", label: "未完成" },
  { value: "failed", label: "失败" },
]);
modelDropdown.setOptions([{ value: "", label: "全部模型" }]);

// 每页条数。改它等于换了一种分页方式，回到第 1 页。
// 用回调传进来的 value，不读 pageSizeDropdown.value——那是在它自己的
// 初始化表达式里引用自己，虽然运行时不会出错，但读起来像有陷阱
const pageSizeDropdown = createDropdown($("page-size"), (value) => {
  pageSize = Number(value) || 10;
  currentPage = 1;
  render();
});
// 默认值 10 必须在选项里，否则 setOptions 会因「当前值不在列表中」退回第一项
pageSizeDropdown.setOptions([
  { value: "10", label: "10" },
  { value: "20", label: "20" },
  { value: "30", label: "30" },
  { value: "40", label: "40" },
  { value: "50", label: "50" },
  { value: "100", label: "100" },
]);
pageSize = Number(pageSizeDropdown.value) || 10;

// ---- 事件绑定 ----
queryBtn.addEventListener("click", runQuery);
searchInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); runQuery(); }   // 回车等同点查询
});
clearBtn.addEventListener("click", clearQuery);
refreshBtn.addEventListener("click", refreshRuns);
searchInput.addEventListener("input", onConditionEdit);
searchClear.addEventListener("click", clearKeyword);
dateFrom.addEventListener("change", onConditionEdit);
dateTo.addEventListener("change", onConditionEdit);
sortTime.addEventListener("click", toggleSort);
pagePrev.addEventListener("click", () => gotoPage(currentPage - 1));
pageNext.addEventListener("click", () => gotoPage(currentPage + 1));
newRunBtn.addEventListener("click", openNewRunModal);
modalCancel.addEventListener("click", closeNewRunModal);
modalConfirm.addEventListener("click", submitNewRun);
// 点击遮罩关闭、Esc 关闭
newRunModal.addEventListener("click", (e) => { if (e.target === newRunModal) closeNewRunModal(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !newRunModal.classList.contains("hidden")) closeNewRunModal();
});
newRunInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submitNewRun(); }
});

loadRuns();
