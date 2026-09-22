"use strict";

// ---- 状态 ----
let currentRunId = null;
let lastEventId = null;       // 增量轮询游标
let events = [];              // 当前 run 的全部事件
let currentRun = null;        // 当前 run 的元信息（推导日志时要用到 model）
let pollTimer = null;
let selectedEventId = null;

const $ = (id) => document.getElementById(id);
const backBtn = $("back-btn");
const deleteBtn = $("delete-btn");
const traceTree = $("trace-tree");
const logStream = $("log-stream");
const contextView = $("context-view");
const statusBar = $("status-bar");
const approvalBar = $("approval-bar");

// ---- 图标映射 ----
const ICONS = {
  run_start: "▶", run_end: "■",
  llm_call: "◆", llm_response: "◇",
  tool_call: "🔧", tool_result: "✅",
  approval_request: "⏸", approval_decision: "✔",
  llm_retry: "↻",
  context_compacted: "🗜",
  log: "·", error: "✖",
};

const STATUS_TEXT = {
  running: "运行中",
  awaiting_approval: "待审批",
  completed: "已完成",
  incomplete: "未完成",
  failed: "失败",
};

// 结束方式的中文说明：状态只说明"有没有结果"，
// ended_by 才说明"怎么结束的"——真做完了，还是半途停下
const ENDED_BY_LABEL = {
  finish_tool: "模型主动声明完成",
  text_response: "模型直接给出回答（未调用 finish）",
  iteration_cap: "跑满迭代次数仍未得出结论",
  empty_output: "模型返回空内容",
  error: "运行出错",
};

// 还在进行中的状态：轮询遇到这些要继续。
// 用「存活状态白名单」而不是「终态黑名单」——黑名单一旦漏写某个状态，
// 页面会永久停止轮询且无法自行恢复（没有地方会重新 startPolling）。
const LIVE_STATUS = new Set(["running", "awaiting_approval"]);

// ---- 启动 / 加载 ----
async function init() {
  setupColumnResize();
  await Systems.load();   // 系统注册表；拿不到会退化成显示原始 key，不阻塞加载
  const runId = new URLSearchParams(location.search).get("id");
  if (!runId) {
    setStatus("缺少 run id，请从列表页进入");
    return;
  }
  await loadRun(runId);
  startPolling();
}

function shorten(s, n) {
  return s && s.length > n ? s.slice(0, n) + "…" : s;
}

// ---- 加载 run 详情 ----
async function loadRun(runId) {
  currentRunId = runId;
  lastEventId = null;
  selectedEventId = null;
  events = [];
  currentRun = null;
  approvalBarSig = null;   // 换了 run，审批条要重新计算
  try {
    const data = await fetchRun(runId);
    events = data.events;
    currentRun = data.run;
    lastEventId = events.length ? events[events.length - 1].id : null;
    renderTrace();
    renderLog();
    renderApprovalBar();
    updateStatusBar(data.run);
  } catch (e) {
    setStatus("加载失败: " + e.message);
    renderTrace();
    renderLog();
    renderApprovalBar();
  }
}

async function fetchRun(runId) {
  const res = await fetch(`/api/runs/${runId}`);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    const e = new Error(err.detail || ("HTTP " + res.status));
    e.notFound = res.status === 404;   // 轮询据此区分「被删除」与「网络抖动」
    throw e;
  }
  return await res.json();
}

// ---- 删除当前 run ----
async function deleteRun() {
  if (!currentRunId) { setStatus("没有可删除的记录"); return; }
  if (!confirm("确定删除这条运行记录吗？删除后不可恢复。")) return;
  try {
    const res = await fetch(`/api/runs/${currentRunId}`, { method: "DELETE" });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      setStatus("删除失败: " + (err.detail || res.status));
      return;
    }
    window.location.href = "/";
  } catch (e) {
    setStatus("请求出错: " + e.message);
  }
}

// ---- 轮询 ----
function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(poll, 1000);
}

async function poll() {
  if (!currentRunId) return;
  const url = lastEventId
    ? `/api/runs/${currentRunId}/events?after=${encodeURIComponent(lastEventId)}`
    : `/api/runs/${currentRunId}/events`;
  try {
    const res = await fetch(url);
    const data = await res.json();
    if (data.events && data.events.length) {
      events = events.concat(data.events);
      lastEventId = events[events.length - 1].id;
      renderTrace();
      renderLog();
      renderApprovalBar();
    }
    // 检查 run 是否结束（结束后停止轮询）
    const runData = await fetchRun(currentRunId);
    currentRun = runData.run;
    updateStatusBar(runData.run);
    if (runData.run && !LIVE_STATUS.has(runData.run.status)) {
      stopPolling();
    }
  } catch (e) {
    // run 被删除后 fetchRun 会抛 404。以前这里静默忽略，页面会对着
    // 一条不存在的 run 永远轮询下去，审批条还挂着一个点了没反应的按钮。
    if (e.notFound) {
      stopPolling();
      // 必须强制收起。此时 events 里那条 approval_request 还在，
      // 走 renderApprovalBar() 会因为签名没变而提前返回，条会一直挂着，
      // 上面那个「批准」按钮点下去只会得到 404。
      hideApprovalBar();
      setStatus("该运行记录已被删除");
    }
    /* 其余失败静默忽略（网络抖动等），轮询继续 */
  }
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

function updateStatusBar(run) {
  if (!run) return;
  const status = run.status;
  const label = STATUS_TEXT[status] || status;
  let html = `任务: <b>${escapeHtml(shorten(run.task, 60))}</b>`;
  html += ` <span class="run-status ${escapeHtml(status)}">${escapeHtml(label)}</span>`;
  if (run.source) html += ` · ${escapeHtml(Systems.labelOf(run.source))}`;
  if (run.model) html += ` · ${escapeHtml(run.model)}`;
  if (run.input_tokens || run.output_tokens) {
    html += ` · tokens: ${run.input_tokens}in / ${run.output_tokens}out`;
  }
  // 结束方式：状态只说"有没有结果"，这个才说明"怎么结束的"
  const ended = ENDED_BY_LABEL[run.ended_by];
  if (ended) html += ` · <span class="ended-by">结束方式：${escapeHtml(ended)}</span>`;
  statusBar.innerHTML = html;
}

function setStatus(msg) { statusBar.textContent = msg; }

// ---- 渲染调用路径树 ----
function renderTrace() {
  traceTree.innerHTML = "";
  const byId = {};
  const roots = [];
  for (const e of events) byId[e.id] = e;

  // 建树
  const childrenOf = {};
  for (const e of events) {
    const pid = e.parent_id;
    if (pid && byId[pid]) {
      (childrenOf[pid] = childrenOf[pid] || []).push(e);
    } else {
      roots.push(e);
    }
  }
  const renderNode = (e, depth) => {
    const node = document.createElement("div");
    node.className = "tree-node type-" + e.type;

    const row = document.createElement("div");
    row.className = "tree-row" + (e.id === selectedEventId ? " selected" : "");
    row.style.paddingLeft = (4 + depth * 0) + "px";

    const kids = childrenOf[e.id] || [];
    const toggle = document.createElement("span");
    toggle.className = "tree-toggle";
    toggle.textContent = kids.length ? "▾" : " ";
    row.appendChild(toggle);

    const icon = document.createElement("span");
    icon.className = "tree-icon";
    icon.textContent = ICONS[e.type] || "•";
    row.appendChild(icon);

    const name = document.createElement("span");
    name.className = "tree-name";
    name.textContent = e.name;
    row.appendChild(name);

    const badge = document.createElement("span");
    badge.className = "tree-badge";
    badge.textContent = badgeText(e);
    row.appendChild(badge);

    row.addEventListener("click", () => {
      toggle.textContent = toggle.textContent === "▾" ? "▸" : "▾";
      const childrenEl = node.querySelector(".tree-children");
      if (childrenEl) childrenEl.classList.toggle("collapsed");
      selectEvent(e);
    });

    node.appendChild(row);
    if (kids.length) {
      const childrenEl = document.createElement("div");
      childrenEl.className = "tree-children";
      for (const k of kids) childrenEl.appendChild(renderNode(k, depth + 1));
      node.appendChild(childrenEl);
    }
    return node;
  };

  for (const r of roots) traceTree.appendChild(renderNode(r, 0));
}

function badgeText(e) {
  const m = e.meta || {};
  const parts = [];
  if (e.type === "llm_response") {
    if (m.latency_ms != null) parts.push(m.latency_ms + "ms");
    if (m.input_tokens || m.output_tokens) parts.push(m.input_tokens + "/" + m.output_tokens + "tok");
  }
  if (e.type === "llm_call") {
    if (m.iteration != null) parts.push("iter " + m.iteration);
  }
  // 计划更新的进度徽标：扫一眼调用树就知道做到第几步了
  if (e.type === "tool_call" && e.name === "tool_call: update_plan" && e.input) {
    const total = (e.input.steps || []).length;
    if (total) parts.push(`${e.input.done || 0}/${total} 步`);
  }
  return parts.join(" · ");
}

// ---- 人工审批 ----
// 待审批状态完全由事件流推导，不额外加接口：
// 事件本身已经是审批生命周期的完整有序日志，再加一个接口就是第二个真相来源。
function pendingApprovals(list) {
  const open = new Map();
  for (const e of list) {
    // 只看能在本页处理的那些。canvas 这类外部上报的审批，决定权在它自己的
    // 界面上（用户点击后由那边 resolve），看板这边点了也解不开——
    // 放个按不动的按钮比不显示更糟。
    if (e.type === "approval_request" && isDashboardResolvable()) {
      open.set(e.meta && e.meta.approval_id, e);
    }
  }
  for (const e of list) {
    // 决策事件必须也带 approval_id，否则配不上对，审批条会一直挂着
    if (e.type === "approval_decision") {
      open.delete(e.meta && e.meta.approval_id);
    }
  }
  return [...open.values()];
}

// 这条 run 的审批是不是本页能解的：只有看板自己跑的 run 才注册了
// 等待中的 future，外部上报的 run 点了也只会拿到 409。
//
// 判据必须取 run 的 source——原先读的是 e.meta.source，但没有任何代码
// 往事件 meta 里写过 source（来源只写在 run_start 的 input 里），
// 所以那个判断恒为真、这层防护其实一直没生效。
function isDashboardResolvable() {
  if (!currentRun) return false;      // run 还没加载出来：宁可不显示按钮
  // 空 source 按看板自建处理，与后端历史记录的回填规则一致
  return (currentRun.source || "dashboard") === "dashboard";
}

// 只在内容真正变化时重建。轮询每秒触发一次，
// 无条件重建会把用户刚点下的按钮重新启用，并丢失焦点。
let approvalBarSig = null;

// 强制收起（不看 events 的状态），用于 run 已不存在的场景
function hideApprovalBar() {
  approvalBarSig = null;
  approvalBar.innerHTML = "";
  approvalBar.classList.add("hidden");
}

function renderApprovalBar() {
  const pend = pendingApprovals(events);
  const sig = pend.map((e) => e.meta.approval_id).join(",");
  if (sig === approvalBarSig) return;
  approvalBarSig = sig;

  approvalBar.innerHTML = "";
  if (!pend.length) {
    approvalBar.classList.add("hidden");
    return;
  }

  const ev = pend[0];
  const info = ev.input || {};
  const args = info.arguments || {};
  const content = String(args.content || "");
  const exists = !!info.target_exists;

  const head = document.createElement("div");
  head.className = "approval-head";
  head.innerHTML =
    `<span class="approval-icon">⏸</span>` +
    `<span class="approval-title">agent 请求执行 <code>${escapeHtml(info.tool || "")}</code>，等待你的确认</span>`;
  approvalBar.appendChild(head);

  const detail = document.createElement("div");
  detail.className = "approval-detail";
  detail.innerHTML =
    `<div class="approval-row"><span class="approval-k">目标</span>` +
    `<code class="approval-path">${escapeHtml(info.target || "")}</code>` +
    `<span class="approval-tag ${exists ? "overwrite" : "create"}">` +
    `${exists ? "已存在，将被覆盖" : "新建文件"}</span></div>` +
    `<div class="approval-row"><span class="approval-k">大小</span>` +
    `<span>${info.size_bytes || 0} 字节</span></div>`;
  approvalBar.appendChild(detail);

  const pre = document.createElement("pre");
  pre.className = "approval-preview";
  pre.textContent = content.length > 1200 ? content.slice(0, 1200) + "\n…（已截断）" : content;
  approvalBar.appendChild(pre);

  const actions = document.createElement("div");
  actions.className = "approval-actions";

  const hint = document.createElement("span");
  hint.className = "approval-hint";
  hint.textContent = "超时将自动拒绝";

  const rejectBtn = document.createElement("button");
  rejectBtn.className = "danger";
  rejectBtn.textContent = "拒绝";

  const approveBtn = document.createElement("button");
  approveBtn.className = "primary";
  approveBtn.textContent = "批准执行";

  const decide = (approved) => async () => {
    // 同步禁用两个按钮：否则连点两下会发两个请求，第二个必然拿到 409
    approveBtn.disabled = true;
    rejectBtn.disabled = true;
    try {
      const res = await fetch(
        `/api/runs/${currentRunId}/approvals/${encodeURIComponent(ev.meta.approval_id)}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ approved }),
        }
      );
      if (!res.ok) {
        // 409 = 这次审批已经结束（多半是超时自动拒绝了）。
        // 重新拉一次让界面回到真实状态，而不是把按钮一直禁用着。
        const err = await res.json().catch(() => ({}));
        const msg = "审批未生效: " + (err.detail || res.status);
        await loadRun(currentRunId);
        // 必须放在 loadRun 之后：它内部的 updateStatusBar() 会覆写状态栏，
        // 放前面这条提示一次都显示不出来
        setStatus(msg);
        if (!LIVE_STATUS.has(currentRun && currentRun.status)) stopPolling();
        return;
      }
      setStatus(approved ? "已批准执行" : "已拒绝");
    } catch (e) {
      setStatus("提交审批失败: " + e.message);
      approveBtn.disabled = false;
      rejectBtn.disabled = false;
    }
  };

  approveBtn.addEventListener("click", decide(true));
  rejectBtn.addEventListener("click", decide(false));

  actions.appendChild(hint);
  actions.appendChild(rejectBtn);
  actions.appendChild(approveBtn);
  approvalBar.appendChild(actions);

  approvalBar.classList.remove("hidden");
}

// ---- 渲染日志 ----
// 外部上报的 run 不会带 log 事件（日志要由上报端自己发），
// 这类 run 的日志面板会一直是空的。这里从调用事件推导出等价的日志行，
// 保证面板有内容可看；推导出的行会在顶部标注来源，不与真实日志混淆。
function deriveLogs(list) {
  const out = [];
  let llmCount = 0;

  for (const e of list) {
    const ts = e.wall_ts;
    switch (e.type) {
      case "run_start":
        out.push({ ts, level: "info", msg: `启动运行${currentRun && currentRun.model ? "，模型=" + currentRun.model : ""}` });
        break;
      case "llm_call":
        llmCount++;
        out.push({ ts, level: "info", msg: `第 ${llmCount} 次 LLM 调用` });
        break;
      case "tool_call": {
        // update_plan 表示"进度推进了"，和普通工具调用分开写，
        // 否则混在「调用工具 X」里一眼看不出任务做到哪
        if (e.name === "tool_call: update_plan") {
          const inp = e.input || {};
          out.push({
            ts, level: "info",
            msg: `更新计划：${inp.goal || ""}（${inp.done || 0}/${(inp.steps || []).length} 步）`,
          });
        } else {
          out.push({ ts, level: "info", msg: `调用工具 ${toolName(e.name)}` });
        }
        break;
      }
      case "approval_request": {
        const t = (e.input && e.input.tool) || "";
        const tg = (e.input && e.input.target) || "";
        out.push({ ts, level: "warn", msg: `等待人工审批：${t} → ${tg}` });
        break;
      }
      case "approval_decision": {
        const d = e.output || {};
        out.push({
          ts,
          level: d.approved ? "info" : "warn",
          msg: `审批结果：${d.approved ? "已批准" : "已拒绝"}（${d.decided_by || ""}）${d.reason || ""}`,
        });
        break;
      }
      case "llm_retry": {
        const m = e.meta || {};
        out.push({
          ts, level: "warn",
          msg: `LLM 调用失败，${m.delay_s || "?"}s 后重试（第 ${m.attempt || "?"} 次）`,
        });
        break;
      }
      case "context_compacted": {
        const m = e.meta || {};
        out.push({
          ts, level: "warn",
          msg: `上下文达 ${m.prompt_tokens_before || "?"} tokens，`
             + `已把 ${m.messages_before || "?"} 条消息压缩为 ${m.messages_after || "?"} 条`,
        });
        break;
      }
      case "error":
        out.push({ ts, level: "error", msg: e.name || "运行出错" });
        break;
      case "run_end":
        out.push({ ts, level: "info", msg: "运行结束" });
        break;
    }
  }
  return out;
}

// 事件名形如 "tool_call: saveCanvas"，取出工具名
function toolName(name) {
  const m = /^tool_call:\s*(.+)$/.exec(name || "");
  return m ? m[1] : (name || "");
}

function logLine(ts, level, msg) {
  const line = document.createElement("div");
  line.className = "log-line";
  line.innerHTML =
    `<span class="log-time">${escapeHtml((ts || "").slice(11))}</span>` +
    `<span class="log-level ${level}">${level}</span>` +
    `<span class="log-msg">${escapeHtml(msg)}</span>`;
  return line;
}

function emptyHint(text) {
  const d = document.createElement("div");
  d.className = "empty-hint";
  d.textContent = text;
  return d;
}

function renderLog() {
  logStream.innerHTML = "";

  const real = events.filter((e) => e.type === "log" || e.type === "error");

  if (real.length) {
    for (const l of real) {
      const level = (l.meta && l.meta.level) || (l.type === "error" ? "error" : "info");
      logStream.appendChild(logLine(l.wall_ts, level, l.name));
    }
  } else {
    // 没有上报日志，退而从调用事件推导
    const derived = deriveLogs(events);
    if (derived.length) {
      const note = document.createElement("div");
      note.className = "log-note";
      note.textContent = "该运行未上报日志，以下条目由调用事件推导";
      logStream.appendChild(note);
      for (const d of derived) logStream.appendChild(logLine(d.ts, d.level, d.msg));
    } else {
      logStream.appendChild(
        emptyHint(events.length ? "该运行没有可显示的日志" : "暂无日志")
      );
    }
  }

  logStream.scrollTop = logStream.scrollHeight;
}

// ---- 渲染上下文 ----
function selectEvent(e) {
  selectedEventId = e.id;
  // 高亮
  document.querySelectorAll(".tree-row").forEach((r) => r.classList.remove("selected"));
  renderContext(e);
}

function renderContext(e) {
  const m = e.meta || {};

  // 构建 id -> event 映射，用于查找父节点
  const byId = {};
  for (const ev of events) byId[ev.id] = ev;

  // 计算"有效输入"：优先自身 input；否则沿 parent 链找最近的父节点 input
  let inputSourceId = null;
  let inputValue = undefined;
  let node = e;
  while (node) {
    if (node.input !== undefined && node.input !== null) {
      inputSourceId = node.id;
      inputValue = node.input;
      break;
    }
    node = node.parent_id ? byId[node.parent_id] : null;
  }

  contextView.innerHTML = "";

  // 事件
  contextView.appendChild(makeJsonSection("事件", {
    id: e.id, type: e.type, name: e.name, time: e.wall_ts,
  }));

  // INPUT：始终显示；无自身输入时继承父节点并标注来源
  const inputLabel = inputSourceId === e.id
    ? "INPUT"
    : `INPUT（继承自 ${byId[inputSourceId] ? byId[inputSourceId].type : "父节点"}）`;
  const notice = truncationNotice(e, inputValue);
  if (notice) contextView.appendChild(notice);
  contextView.appendChild(makeJsonSection(
    inputLabel,
    inputValue !== undefined ? inputValue : "(无输入)"
  ));

  // OUTPUT：有则显示
  if (e.output !== undefined && e.output !== null) {
    contextView.appendChild(makeJsonSection("OUTPUT", e.output));
  }

  // META：有则显示
  if (Object.keys(m).length) {
    contextView.appendChild(makeJsonSection("META", m));
  }

  if (!contextView.children.length) {
    contextView.innerHTML = '<div class="empty-hint">该事件无内容</div>';
  }
}

// 输入被截断时给一条显眼的提示。
//
// 为什么要专门标出来：被截掉的往往正是决定 AI 行为的那部分输入
// （比如 canvas 上报里含完整画布 state 的 system prompt），
// 而截断标记混在几千行 JSON 中间，不主动提示就会被当成完整内容来解读。
function truncationNotice(e, inputValue) {
  const truncatedByFlag = (e.meta || {}).input_truncated === true;
  const originalChars = (e.meta || {}).input_original_chars;
  // 旧数据没有 meta 标记，退回到探测内容里的截断标记
  const truncatedByMarker = !truncatedByFlag && containsTruncationMarker(inputValue);
  if (!truncatedByFlag && !truncatedByMarker) return null;

  const d = document.createElement("div");
  d.className = "truncation-notice";
  const size = originalChars
    ? `原始输入约 ${originalChars.toLocaleString()} 字符`
    : "原始输入比这里显示的大得多";
  d.textContent = `⚠ 这条 INPUT 被截断了，${size}。`
    + "下面的内容只是开头，不足以还原模型当时看到的全部上下文。";
  return d;
}

function containsTruncationMarker(value) {
  if (typeof value === "string") return value.includes("…(truncated)");
  if (Array.isArray(value)) return value.some(containsTruncationMarker);
  if (value && typeof value === "object") {
    return Object.values(value).some(containsTruncationMarker);
  }
  return false;
}

// 创建一个标题 + 可折叠 JSON 树的区块
function makeJsonSection(title, value) {
  const sec = document.createElement("div");
  sec.className = "context-section";
  const h = document.createElement("h4");
  h.textContent = title;
  sec.appendChild(h);
  sec.appendChild(buildJsonNode(value, null));
  return sec;
}

// 递归构建可折叠的 JSON 树节点
function buildJsonNode(value, key) {
  const isContainer = value !== null && typeof value === "object";

  // 标量：一行显示 key: value
  if (!isContainer) {
    const line = document.createElement("div");
    line.className = "json-line";
    if (key !== null && key !== undefined) {
      const k = document.createElement("span");
      k.className = "json-key";
      k.textContent = JSON.stringify(key) + ": ";
      line.appendChild(k);
    }
    line.appendChild(scalarSpan(value));
    return line;
  }

  const isArr = Array.isArray(value);
  const entries = isArr ? value.map((v, i) => [i, v]) : Object.entries(value);

  const container = document.createElement("div");
  container.className = "json-obj";

  // 首行（含折叠箭头 + 键 + 开括号 + 摘要）
  const head = document.createElement("div");
  head.className = "json-line json-head";

  const toggle = document.createElement("span");
  toggle.className = "json-toggle";
  toggle.textContent = entries.length ? "▾" : " ";
  head.appendChild(toggle);

  if (key !== null && key !== undefined) {
    const k = document.createElement("span");
    k.className = "json-key";
    k.textContent = JSON.stringify(key) + ": ";
    head.appendChild(k);
  }

  const open = document.createElement("span");
  open.className = "json-brace";
  open.textContent = isArr ? "[" : "{";
  head.appendChild(open);

  const summary = document.createElement("span");
  summary.className = "json-summary";
  summary.textContent = entries.length
    ? ` ${entries.length} ${isArr ? "项" : "个字段"} `
    : "";
  head.appendChild(summary);

  container.appendChild(head);

  // 子节点
  const children = document.createElement("div");
  children.className = "json-children";
  for (const [k, v] of entries) {
    children.appendChild(buildJsonNode(v, k));
  }
  container.appendChild(children);

  // 闭合括号
  const close = document.createElement("div");
  close.className = "json-line json-close";
  const closeBrace = document.createElement("span");
  closeBrace.className = "json-brace";
  closeBrace.textContent = isArr ? "]" : "}";
  close.appendChild(closeBrace);
  container.appendChild(close);

  // 折叠/展开交互
  if (entries.length) {
    const setCollapsed = (c) => {
      toggle.textContent = c ? "▸" : "▾";
      children.style.display = c ? "none" : "";
      close.style.display = c ? "none" : "";
      summary.style.display = c ? "" : "";
    };
    head.addEventListener("click", () => setCollapsed(children.style.display !== "none"));
  }

  return container;
}

// 标量值按类型着色
function scalarSpan(value) {
  const span = document.createElement("span");
  if (value === null) {
    span.className = "json-null";
    span.textContent = "null";
  } else if (typeof value === "string") {
    span.className = "json-string";
    span.textContent = JSON.stringify(value);
  } else if (typeof value === "number") {
    span.className = "json-number";
    span.textContent = String(value);
  } else if (typeof value === "boolean") {
    span.className = "json-bool";
    span.textContent = String(value);
  } else {
    span.className = "json-string";
    span.textContent = String(value);
  }
  return span;
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

// ---- 面板宽度拖拽调整 ----
function setupColumnResize() {
  const layout = document.querySelector(".layout");
  const panels = layout.querySelectorAll(".panel");
  const dividers = layout.querySelectorAll(".col-divider");
  const MIN = 200; // 单列最小宽度（px）

  dividers.forEach((divider, i) => {
    divider.addEventListener("mousedown", (e) => {
      e.preventDefault();
      const leftPanel = panels[i];
      const rightPanel = panels[i + 1];
      const startX = e.clientX;
      const leftW = leftPanel.getBoundingClientRect().width;
      const rightW = rightPanel.getBoundingClientRect().width;
      const totalW = leftW + rightW;

      divider.classList.add("active");
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";

      const onMove = (ev) => {
        const dx = ev.clientX - startX;
        const newLeft = Math.min(Math.max(leftW + dx, MIN), totalW - MIN);
        leftPanel.style.flex = "0 0 " + newLeft + "px";
        rightPanel.style.flex = "0 0 " + (totalW - newLeft) + "px";
      };

      const onUp = () => {
        divider.classList.remove("active");
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
      };

      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    });
  });
}

// ---- 事件绑定 ----
backBtn.addEventListener("click", () => { window.location.href = "/"; });
deleteBtn.addEventListener("click", deleteRun);

init();
