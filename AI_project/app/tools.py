"""示例工具：无第三方依赖、有安全边界。

只读工具（作用域：项目根目录）
- calculator        用 ast 安全解析算术表达式
- get_current_time  返回当前时间
- list_files        列出目录内容
- read_file         读取文件内容

有副作用的工具（作用域：workspace/ 子目录，且需人工审批）
- write_file        写入文本文件

读写在作用域上是刻意不对称的：读放大到整个项目根，写则收窄到 workspace/。
写操作的风险远高于读，所以边界更紧——即便人工审批被习惯性放行，
agent 也动不了 app/ 下的源码和 .env。
"""

from __future__ import annotations

import ast
import operator
import os
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# agent 能写东西的范围。可用 WORKSPACE_DIR 环境变量覆盖——
# 演示服务靠它把工作目录也隔离出去。只隔离 traces 是不够的：
# MEMORY.md / PLAN.md 是 agent 后续运行的**输入**，
# 假演示写进去的东西会被真实 agent 当成自己的记忆读出来。
_ws = os.getenv("WORKSPACE_DIR")
WORKSPACE_ROOT = Path(_ws).resolve() if _ws else PROJECT_ROOT / "workspace"

# 这些工具在执行前必须先经过人工审批（见 app/approval.py）
APPROVAL_REQUIRED_TOOLS = frozenset({"write_file"})

# 单次写入的字节上限
MAX_WRITE_BYTES = 100_000

# 长期记忆：固定的单个文件，位置不可由模型指定（见 remember 的说明）
MEMORY_PATH = WORKSPACE_ROOT / "MEMORY.md"
MAX_NOTE_CHARS = 2000      # 单条记忆长度上限
MAX_MEMORY_CHARS = 8000    # 一次 recall 返回的内容上限，避免撑爆上下文

# 当前任务的计划。和 MEMORY 分开：记忆是跨任务的零散结论，
# 计划是「这次要做到哪、现在到哪了」。混在一起两者都会变脏。
PLAN_PATH = WORKSPACE_ROOT / "PLAN.md"
MAX_PLAN_STEPS = 30        # 步骤数上限，防止模型列出一百条谁也看不完的清单

# ---- 提供给 OpenAI 兼容 SDK 的工具定义（function calling 格式） ----
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "计算一个算术表达式的值。支持 + - * / // % ** 以及括号，仅接受数字与运算符。",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "算术表达式，例如 (34*12+8)/7"}
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "获取当前日期和时间。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出指定目录下的文件与子目录（限定在项目根目录内）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对于项目根目录的路径，例如 . 或 app"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取项目根目录内某个文本文件的内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对于项目根目录的文件路径"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": (
                "声明任务已完成，并给出最终答复。当你确信已经满足用户的要求时调用它。"
                "这是结束任务的正常方式；如果还没做完就不要调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "answer": {"type": "string", "description": "给用户的最终回答"},
                    "summary": {
                        "type": "string",
                        "description": "可选：一句话说明你做了哪些操作",
                    },
                },
                "required": ["answer"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_plan",
            "description": (
                "声明或更新当前任务的执行计划。任务需要多步才能完成时，"
                "先规划再动手；每完成一步就再调用一次，把 done 往前推。"
                "计划会落到文件里，所以即使这次没做完，下次运行也能接着做。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string", "description": "这次任务要达成的一句话目标"},
                    "steps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "有序的步骤清单，每条是一句可判断完成与否的话",
                    },
                    "done": {
                        "type": "integer",
                        "description": "已经完成的步骤数，即 steps 里前几条已完成。刚开始规划时填 0",
                    },
                },
                "required": ["goal", "steps"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": (
                "把一条信息写进长期记忆，供以后的运行使用。适用于："
                "用户的偏好、已完成工作的结论、需要跨任务延续的进度。"
                "不要记录临时的中间步骤。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "note": {
                        "type": "string",
                        "description": "要记住的内容，写成一句能独立看懂的话",
                    }
                },
                "required": ["note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall",
            "description": (
                "读取长期记忆。不知道以前做过什么时，先用它查一下；"
                "不确定存过哪些相关内容时，也可以不带关键词读取全部。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "可选的关键词，只返回包含它的条目；留空返回全部",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "把文本内容写入 workspace/ 目录下的文件（不存在则创建，存在则覆盖）。"
                "该操作有副作用，会先请求人工审批；若用户拒绝，不要重试，"
                "请直接说明情况或改用其它方式。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "相对于 workspace/ 的文件路径，例如 notes/result.txt",
                    },
                    "content": {"type": "string", "description": "要写入的完整文本内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
]

# 工具名 -> 实现函数
TOOL_IMPLEMENTATIONS = {}


def _register(name: str):
    def deco(fn):
        TOOL_IMPLEMENTATIONS[name] = fn
        return fn
    return deco


# ---- 安全解析算术表达式 ----
_ALLOWED_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_ALLOWED_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _eval_node(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
        return _ALLOWED_BINOPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARY:
        return _ALLOWED_UNARY[type(node.op)](_eval_node(node.operand))
    raise ValueError(f"不支持的表达式节点: {type(node).__name__}")


@_register("calculator")
def calculator(expression: str) -> dict:
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as e:
        return {"error": f"表达式语法错误: {e}"}
    try:
        value = _eval_node(tree)
    except (ValueError, ZeroDivisionError) as e:
        return {"error": str(e)}
    # 整数结果不显示多余小数
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return {"expression": expression, "result": value}


@_register("get_current_time")
def get_current_time() -> dict:
    return {
        "datetime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "timezone": time.strftime("%Z", time.localtime()),
    }


def _resolve_within_root(path: str) -> Path:
    """把相对路径解析到项目根目录内，越界则抛错。"""
    p = (PROJECT_ROOT / path).resolve()
    if not p.is_relative_to(PROJECT_ROOT.resolve()):
        raise ValueError(f"路径越界，禁止访问: {path}")
    return p


@_register("list_files")
def list_files(path: str = ".") -> dict:
    try:
        p = _resolve_within_root(path)
    except ValueError as e:
        return {"error": str(e)}
    if not p.exists():
        return {"error": f"路径不存在: {path}"}
    if not p.is_dir():
        return {"error": f"不是目录: {path}"}
    entries = []
    for child in sorted(p.iterdir()):
        kind = "dir" if child.is_dir() else "file"
        entries.append({"name": child.name, "type": kind})
    return {"path": path, "entries": entries}


@_register("read_file")
def read_file(path: str) -> dict:
    try:
        p = _resolve_within_root(path)
    except ValueError as e:
        return {"error": str(e)}
    if not p.exists():
        return {"error": f"文件不存在: {path}"}
    if not p.is_file():
        return {"error": f"不是文件: {path}"}
    try:
        content = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        content = p.read_bytes()[:2000].decode("utf-8", errors="replace")
        content += "\n...(二进制文件，已截断)"
    # 限制长度，避免撑爆上下文
    if len(content) > 8000:
        content = content[:8000] + "\n...(内容过长，已截断)"
    return {"path": path, "content": content}


def _memory_entries() -> list[str]:
    """读出记忆文件里的条目行。"""
    if not MEMORY_PATH.exists():
        return []
    try:
        text = MEMORY_PATH.read_text(encoding="utf-8")
    except OSError:
        return []
    return [ln for ln in text.splitlines() if ln.startswith("- [")]


@_register("remember")
def remember(note: str) -> dict:
    """把一条信息写进长期记忆，供以后的运行使用。

    刻意不接受 path 参数——记忆只能写到固定的 workspace/MEMORY.md。
    一旦让它能指定路径，这个「不需要审批」的工具就成了任意文件写入的后门。
    也正因为写的是 agent 自己的笔记本（而不是用户的世界），它不需要人工审批：
    每一步都弹窗会让记忆功能根本没法用，agent 会干脆不用它。
    """
    note = (note or "").strip()
    if not note:
        return {"error": "note 不能为空"}
    if len(note) > MAX_NOTE_CHARS:
        return {"error": f"单条记忆过长（{len(note)} 字符，上限 {MAX_NOTE_CHARS}）"}

    try:
        MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        # 追加而不是覆盖：记忆是一份流水，不是可变的配置
        with MEMORY_PATH.open("a", encoding="utf-8") as f:
            f.write(f"- [{time.strftime('%Y-%m-%d %H:%M')}] {note}\n")
    except OSError as e:
        return {"error": f"写入记忆失败: {e}"}

    return {
        "remembered": note,
        "entries_total": len(_memory_entries()),
        "path": "workspace/MEMORY.md",
    }


@_register("recall")
def recall(query: str = "") -> dict:
    """读取长期记忆。query 为空返回全部，否则只返回包含该关键词的条目。"""
    entries = _memory_entries()
    if query and query.strip():
        q = query.strip().lower()
        entries = [e for e in entries if q in e.lower()]

    if not entries:
        return {
            "entries": [],
            "count": 0,
            "note": "没有匹配的记忆" if query else "记忆为空",
        }

    text = "\n".join(entries)
    truncated = len(text) > MAX_MEMORY_CHARS
    return {
        "entries": entries,
        "count": len(entries),
        "content": text[:MAX_MEMORY_CHARS],
        "truncated": truncated,
    }


def load_plan() -> str | None:
    """读出当前计划文件的内容；没有则返回 None。供运行开始时注入系统提示词。"""
    if not PLAN_PATH.exists():
        return None
    try:
        text = PLAN_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


@_register("update_plan")
def update_plan(goal: str, steps: list[str], done: int = 0) -> dict:
    """声明或更新当前任务的计划。

    为什么要把计划外化成文件，而不是只让模型在脑子里记：
    - 它让「跑到迭代上限还没做完」变成可续的——计划留在文件里，下次运行接着做；
    - 它让进度可被观察，而不是只能从一堆工具调用里猜；
    - 逼模型在动手前先把任务拆开，而不是走一步看一步。

    同样不接受 path 参数（理由见 remember）：一个免审批的写工具，
    路径必须锁死。
    """
    goal = (goal or "").strip()
    if not goal:
        return {"error": "goal 不能为空"}
    steps = [str(s).strip() for s in (steps or []) if str(s).strip()]
    if not steps:
        return {"error": "steps 不能为空"}
    if len(steps) > MAX_PLAN_STEPS:
        return {"error": f"步骤过多（{len(steps)} 条，上限 {MAX_PLAN_STEPS}）"}
    if not (0 <= done <= len(steps)):
        return {"error": f"done 越界：应在 0~{len(steps)} 之间，收到 {done}"}

    lines = [
        f"# 计划：{goal}",
        "",
        f"更新于 {time.strftime('%Y-%m-%d %H:%M')}",
        "",
    ]
    for i, s in enumerate(steps):
        lines.append(f"- [{'x' if i < done else ' '}] {s}")
    lines.append("")

    try:
        PLAN_PATH.parent.mkdir(parents=True, exist_ok=True)
        PLAN_PATH.write_text("\n".join(lines), encoding="utf-8")
    except OSError as e:
        return {"error": f"写入计划失败: {e}"}

    return {
        "goal": goal,
        "steps": steps,
        "done": done,
        "remaining": steps[done:],
        "finished": done >= len(steps),
        "path": "workspace/PLAN.md",
    }


@_register("finish")
def finish(answer: str, summary: str = "") -> dict:
    """声明任务完成。由 agent 循环识别并据此结束运行，不会真的"执行"什么。

    它存在的意义：把「模型不再调用工具」和「模型明确说做完了」区分开。
    前者是个副作用，模型卡住、被截断、干脆放弃都会表现成它；
    后者才是一次真正的完成声明。
    """
    return {"finished": True, "answer": answer, "summary": summary}


def resolve_write_target(path: str) -> Path:
    """把相对路径解析到 workspace/ 内，越界则抛 ValueError。

    用 .resolve() 先展开，所以符号链接逃逸（workspace/link -> ..）也会被挡住。
    审批前会先调一次这个函数：路径本来就不合法的话，
    没必要去打扰人点「批准」再告诉他「路径越界」。
    """
    p = (WORKSPACE_ROOT / path).resolve()
    if not p.is_relative_to(WORKSPACE_ROOT.resolve()):
        raise ValueError(f"路径越界，write_file 只能写入 workspace/ 目录: {path}")
    return p


@_register("write_file")
def write_file(path: str, content: str) -> dict:
    try:
        p = resolve_write_target(path)
    except ValueError as e:
        # 与审批前的校验重复，是刻意的纵深防御：审批不能替代沙箱
        return {"error": str(e)}

    size = len(content.encode("utf-8"))
    if size > MAX_WRITE_BYTES:
        return {"error": f"内容过大（{size} 字节，上限 {MAX_WRITE_BYTES} 字节）"}

    existed = p.exists()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    except OSError as e:
        return {"error": f"写入失败: {e}"}

    return {
        "path": str(p.relative_to(PROJECT_ROOT)).replace("\\", "/"),
        "bytes": size,
        "overwritten": existed,   # 覆盖已有文件与新建文件，审阅时含义不同
        "written": True,
    }


def call_tool(name: str, arguments: dict) -> dict:
    """按名称调用工具，返回结果 dict（内部异常转为 error 字段）。"""
    fn = TOOL_IMPLEMENTATIONS.get(name)
    if fn is None:
        return {"error": f"未知工具: {name}"}
    try:
        return fn(**arguments) or {}
    except TypeError as e:
        return {"error": f"工具入参错误: {e}"}
    except Exception as e:
        return {"error": f"工具执行异常: {e}"}
