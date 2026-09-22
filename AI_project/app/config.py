"""读取 .env 配置，初始化 DeepSeek 客户端（OpenAI 兼容协议）。"""

from __future__ import annotations

import os
import re

from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

# 密钥：优先 DEEPSEEK_API_KEY，兼容旧的 ANTHROPIC_API_KEY 命名
API_KEY = os.getenv("DEEPSEEK_API_KEY") or os.getenv("ANTHROPIC_API_KEY", "")
# 模型：优先 DEEPSEEK_MODEL，兼容旧的 ANTHROPIC_MODEL 命名
MODEL = os.getenv("DEEPSEEK_MODEL") or os.getenv("ANTHROPIC_MODEL", "deepseek-v4-flash")
# DeepSeek 兼容端点（OpenAI 协议）
BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))

# 工具审批超时（秒）：超时无人响应则自动拒绝，避免任务永久挂起
APPROVAL_TIMEOUT_S = float(os.getenv("APPROVAL_TIMEOUT_S", "300"))

# ---- 接入的系统（run 的来源）----
# 一个 run 要么是看板自己新建任务跑出来的，要么是外部项目（canvas 等）
# 通过 /api/ingest 上报的。source 就是这条区分的依据，取值即下表的 key。

# 看板自建 run 的固定 source。多处要拿它做判断（审批按钮归属、统计兜底），
# 所以提成常量，避免各处再写字符串字面量。
DASHBOARD_SOURCE = "dashboard"

# 内置注册表。为什么内置而不只靠 .env：dashboard 必然存在（看板总在跑自己的
# 任务），要求运维记得写进 .env 只会让默认状态下列表里冒出一个没名字的来源。
# canvas / canvas-core 是仅有的两个外部接入方，一并内置，开箱就有中文名。
# 两者是并行的设计器，上报同一套事件，只能靠 source 区分。
DEFAULT_SYSTEMS = {
    DASHBOARD_SOURCE: "AI-Dashboard-Agent",
    "canvas": "Canvas 设计器",
    "canvas-core": "Canvas Core 设计器",
}


def _parse_systems(raw: str) -> dict[str, str]:
    """解析 .env 里的 SYSTEMS，格式 `key:显示名,key:显示名`。

    宽松优先：配置写错时宁可少一行显示名，也不能让服务起不来。
    - 中英文逗号都当分隔符
    - 只按**第一个**冒号切，显示名里可以再带冒号（canvas:Canvas: 设计器）
    - 漏写显示名的项退化成 key 当显示名，不报错
    """
    out: dict[str, str] = {}
    for item in raw.replace("，", ",").split(","):
        item = item.strip()
        if not item:
            continue
        # 用 re.split 定位第一个中/英文冒号，而不是把全角冒号整体替换成半角：
        # 后者会把显示名里的冒号也一起改写（"bar:Bar：项目" 会变成 "Bar:项目"）
        parts = re.split(r"[:：]", item, maxsplit=1)
        key = parts[0].strip()
        label = parts[1].strip() if len(parts) > 1 else ""
        if key:
            out[key] = label or key
    return out


# 最终注册表：内置项在前（决定下拉里的顺序），.env 可覆盖显示名或追加新系统。
# 未注册的 source 不会被拒绝——ingest 照常收，前端拿原始 key 显示。
SYSTEMS: dict[str, str] = {**DEFAULT_SYSTEMS, **_parse_systems(os.getenv("SYSTEMS", ""))}

# ---- 上下文预算 ----
# 模型能吃下多少 token。deepseek-v4-flash / v4-pro 官方标称 1M
# （见 https://api-docs.deepseek.com/zh-cn/news/news260424/ ，最大输出 393,216）。
# 换模型时务必改这里。
MODEL_CONTEXT_WINDOW = int(os.getenv("MODEL_CONTEXT_WINDOW", "1000000"))

# 实际可用的比例。为什么不贴着窗口设：
#   1. 压缩本身是一次 LLM 调用，摘要加上保留的最近消息仍要占空间；
#   2. 模型输出也占窗口；
#   3. 你只在发起调用之前能控制，必须给"这一轮还会加多少"留余量；
#   4. 即使在窗口内，超长上下文的实际效果也会下降——预算不只是防崩溃，
#      也是把对话按住在模型表现还好的区间里。
CONTEXT_BUDGET_RATIO = float(os.getenv("CONTEXT_BUDGET_RATIO", "0.6"))

# 显式指定 CONTEXT_BUDGET_TOKENS 时以它为准（演示、压测用），否则由窗口倒推。
# 当前默认 1_000_000 × 0.6 = 600_000。
CONTEXT_BUDGET_TOKENS = int(
    os.getenv("CONTEXT_BUDGET_TOKENS") or int(MODEL_CONTEXT_WINDOW * CONTEXT_BUDGET_RATIO)
)

# 压缩时至少原样保留最近多少条消息（越近的越可能正在被使用）
CONTEXT_KEEP_RECENT = int(os.getenv("CONTEXT_KEEP_RECENT", "6"))

# 单例客户端（无 key 时为 None，便于未配置时友好报错）
#
# max_retries=0：关掉 SDK 的隐式重试，改由 agent.py 自己实现。
# SDK 的重试在看板上完全不可见——一次"成功"的调用背后可能悄悄重试过两次，
# 而这个项目的意义就是让这类过程可见。
client: AsyncOpenAI | None = None
if API_KEY:
    client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL, max_retries=0)
