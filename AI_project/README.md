# AI-Dashboard-Agent

一个简单的 AI agent 项目，重点在于**可观测性**：你能实时查看 agent 的：

- **调用路径** —— LLM 调用 → 工具调用 → 结果的层级链路
- **日志** —— 带时间戳的运行日志
- **上下文** —— 每一步发给模型的 messages、工具入参、返回结果

技术栈：Python + FastAPI + OpenAI 兼容 SDK（DeepSeek）+ 单页 Web 看板。

## 快速开始

```bash
# 1. 创建并激活虚拟环境
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置环境变量
cp .env.example .env      # 然后编辑 .env，填入 ANTHROPIC_API_KEY

# 4. 启动 Web 看板
python run.py
```

浏览器打开 <http://127.0.0.1:8000>，输入任务，点击 **Run**，即可实时看到调用路径树、日志流与上下文。

## CLI 单次运行（不启动 Web）

```bash
python run.py "计算 (34*12+8)/7 并告诉我当前时间"
```

终端会打印调用路径树、日志和最终答案。

## 目录结构

```
run.py               # 入口：默认 Web，带参数走 CLI
app/
├── config.py        # 读取 .env，初始化客户端
├── tracer.py        # 观测层：TraceStore / Run / TraceEvent
├── tools.py         # 示例工具（计算器/时间/文件读取）
├── agent.py         # agent 循环
├── main.py          # FastAPI 路由 + 静态页面
└── static/          # 看板前端（index.html / app.js / style.css）
```

## 三个视图说明

| 视图 | 对应事件 | 说明 |
|------|----------|------|
| 调用路径 | `run_start` / `llm_call` / `tool_call` / `tool_result` / `run_end` | `parent_id` 串成的树，展示完整链路 |
| 日志 | `log` / `error` | 带时间戳与级别的日志流 |
| 上下文 | 任意事件的 `input` / `output` | 点击节点查看 messages、工具入参、返回结果的 JSON |

每次运行结束会把完整轨迹落盘到 `traces/<run_id>.json`，重启后仍可在看板查看历史 run。

## 配置项（.env）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DEEPSEEK_API_KEY` | — | 必填 |
| `DEEPSEEK_MODEL` | `deepseek-v4-flash` | 可切换为 `deepseek-v4-pro` 等 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容端点 |
| `HOST` | `127.0.0.1` | Web 监听地址 |
| `PORT` | `8000` | Web 监听端口 |
| `SYSTEMS` | 内置 `dashboard` / `canvas` | 接入的系统注册表，见下 |

> `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` 仍可作为旧命名被读取，但新的配置请用 `DEEPSEEK_*`。

## 接入新系统

运行记录按**来源系统**区分：看板自己新建的任务算 `dashboard`，
外部项目通过 `/api/ingest` 上报时带自己的 `source`。列表页可按系统筛选，
统计页有「系统分布」图。

接一个新项目只要两步，都不用改代码：

1. 上报时带上自己的 key：

   ```bash
   curl -X POST http://127.0.0.1:8000/api/ingest/runs \
     -H "Content-Type: application/json" \
     -d '{"run_id":"r-1","task":"任务描述","model":"m","source":"billing"}'
   ```

2. 在 `.env` 里给这个 key 配一个显示名（不配也能用，界面会先显示原始 key）：

   ```bash
   SYSTEMS=billing:保单系统,portal:门户站点
   ```

   `SYSTEMS` 不是白名单——没登记的 source 照收不误，
   补上显示名后列表、下拉和统计图会自动变成中文。
