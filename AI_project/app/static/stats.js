"use strict";

// ---- 统计页 ----

const $ = (id) => document.getElementById(id);
const statCards = $("stat-cards");
const statusChart = $("status-chart");
const modelChart = $("model-chart");
const sourceChart = $("source-chart");
const recentTbody = $("recent-tbody");
const statusBar = $("status-bar");

const STATUS_LABEL = {
  running: "运行中",
  awaiting_approval: "待审批",
  completed: "已完成",
  incomplete: "未完成",
  failed: "失败",
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

function fmt(n) {
  if (n == null) return "0";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return String(n);
}

// ---- 加载统计 ----
async function loadStats() {
  try {
    // 先拿系统注册表，图表和列表才显示中文名而不是 canvas
    await Systems.load();
    const res = await fetch("/api/stats");
    const d = await res.json();
    renderCards(d);
    renderStatusChart(d.status_counts || {});
    renderModelChart(d.model_counts || {});
    renderSourceChart(d.source_counts || {});
    renderRecent(d.recent_runs || []);
    setStatus(`共 ${d.total_runs} 条运行记录 · 已加载统计`);
  } catch (e) {
    setStatus("加载统计失败: " + e.message);
  }
}

// ---- 顶部指标卡片 ----
function renderCards(d) {
  const sc = d.status_counts || {};
  const cards = [
    { icon: "🚀", label: "运行总数", value: d.total_runs, color: "accent" },
    { icon: "✅", label: "已完成", value: sc.completed || 0, color: "ok" },
    { icon: "⚠️", label: "未完成", value: sc.incomplete || 0, color: "warn" },
    { icon: "❌", label: "失败", value: sc.failed || 0, color: "err" },
    { icon: "⏸️", label: "待审批", value: sc.awaiting_approval || 0, color: "purple" },
    { icon: "🔄", label: "运行中", value: sc.running || 0, color: "warn" },
    { icon: "🔁", label: "LLM 调用", value: d.total_llm_calls, color: "purple" },
    { icon: "🔧", label: "工具调用", value: d.total_tool_calls, color: "cyan" },
    { icon: "⏱️", label: "平均耗时", value: (d.avg_latency_ms || 0) + "ms", color: "accent" },
    { icon: "🪙", label: "输入 tokens", value: fmt(d.total_input_tokens), color: "cyan" },
    { icon: "📤", label: "输出 tokens", value: fmt(d.total_output_tokens), color: "purple" },
  ];

  statCards.innerHTML = "";
  for (const c of cards) {
    const card = document.createElement("div");
    card.className = "metric-card";
    card.innerHTML =
      `<div class="metric-icon ${c.color}">${c.icon}</div>` +
      `<div class="metric-body">` +
        `<div class="metric-value">${escapeHtml(String(c.value))}</div>` +
        `<div class="metric-label">${c.label}</div>` +
      `</div>`;
    statCards.appendChild(card);
  }
}

// ---- 条形图 ----
function renderStatusChart(counts) {
  const items = [
    { key: "completed", label: "已完成", color: "ok", n: counts.completed || 0 },
    { key: "incomplete", label: "未完成", color: "warn", n: counts.incomplete || 0 },
    { key: "failed", label: "失败", color: "err", n: counts.failed || 0 },
    { key: "awaiting_approval", label: "待审批", color: "purple", n: counts.awaiting_approval || 0 },
    { key: "running", label: "运行中", color: "warn", n: counts.running || 0 },
  ];
  renderBars(statusChart, items);
}

function renderModelChart(counts) {
  const items = Object.entries(counts || {}).map(([m, n]) => ({
    key: m, label: m, color: "accent", n,
  }));
  if (!items.length) {
    modelChart.innerHTML = '<div class="bar-empty">暂无数据</div>';
    return;
  }
  renderBars(modelChart, items);
}

// key → 中文名由 systems.js 统一翻译（未注册的来源原样显示）
function renderSourceChart(counts) {
  const items = Object.entries(counts || {}).map(([k, n]) => ({
    key: k, label: Systems.labelOf(k), color: "cyan", n,
  }));
  if (!items.length) {
    sourceChart.innerHTML = '<div class="bar-empty">暂无数据</div>';
    return;
  }
  renderBars(sourceChart, items);
}

function renderBars(container, items) {
  const max = Math.max(1, ...items.map((i) => i.n));
  container.innerHTML = "";
  for (const it of items) {
    const pct = Math.round((it.n / max) * 100);
    const row = document.createElement("div");
    row.className = "bar-row";
    row.innerHTML =
      `<div class="bar-label">${escapeHtml(it.label)}</div>` +
      `<div class="bar-track"><div class="bar-fill ${it.color}" style="width:${pct}%"></div></div>` +
      `<div class="bar-count">${it.n}</div>`;
    container.appendChild(row);
  }
}

// ---- 最近运行列表 ----
function renderRecent(runs) {
  recentTbody.innerHTML = "";
  if (!runs.length) {
    recentTbody.innerHTML = '<tr><td colspan="6" class="bar-empty">暂无运行记录</td></tr>';
    return;
  }
  for (const r of runs) {
    const tr = document.createElement("tr");
    tr.className = "run-row";
    tr.title = "点击打开详情";
    tr.innerHTML =
      `<td class="col-task" title="${escapeHtml(r.task || "")}">${escapeHtml(shorten(r.task, 50))}</td>` +
      `<td class="col-system">${escapeHtml(Systems.labelOf(r.source))}</td>` +
      `<td class="col-model">${escapeHtml(r.model || "-")}</td>` +
      `<td class="col-status"><span class="run-status ${r.status}">${STATUS_LABEL[r.status] || r.status}</span></td>` +
      `<td class="col-time">${escapeHtml(r.started_at || "-")}</td>` +
      `<td class="col-tokens">${r.input_tokens}/${r.output_tokens}</td>`;
    tr.addEventListener("click", () => {
      window.location.href = "/run?id=" + encodeURIComponent(r.id);
    });
    recentTbody.appendChild(tr);
  }
}

loadStats();
