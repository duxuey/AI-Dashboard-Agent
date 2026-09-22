"use strict";

// ---- 设置页 ----

const $ = (id) => document.getElementById(id);
const configTable = $("config-table");
const statusBar = $("status-bar");

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function setStatus(msg) { statusBar.textContent = msg; }

async function loadConfig() {
  try {
    await Systems.load();
    const res = await fetch("/api/config");
    const c = await res.json();
    renderConfig(c);
    setStatus("已加载当前配置");
  } catch (e) {
    setStatus("加载配置失败: " + e.message);
  }
}

function renderConfig(c) {
  // 接入的系统：列全部注册项，让「新项目接进来要改哪里」一眼可见
  const systems = Systems.list();
  const systemsText = systems.length
    ? systems.map((s) => `${s.label} (${s.key})`).join("、")
    : "-";

  const rows = [
    { icon: "🧠", label: "模型", value: c.model || "-" },
    { icon: "🔗", label: "API 地址", value: c.base_url || "-" },
    { icon: "🌐", label: "监听地址", value: `${c.host}:${c.port}` },
    { icon: "🖥️", label: "接入系统", value: systemsText },
    { icon: "🔑", label: "API 密钥", value: c.api_key_configured ? `已配置 (${c.api_key_preview})` : "未配置", ok: c.api_key_configured },
  ];

  configTable.innerHTML = "";
  for (const r of rows) {
    const row = document.createElement("div");
    row.className = "config-row";
    const statusCls = r.ok === undefined ? "" : (r.ok ? " ok" : " err");
    row.innerHTML =
      `<div class="config-label"><span class="config-icon">${r.icon}</span> ${escapeHtml(r.label)}</div>` +
      `<div class="config-value${statusCls}">${escapeHtml(r.value)}</div>`;
    configTable.appendChild(row);
  }
}

loadConfig();
