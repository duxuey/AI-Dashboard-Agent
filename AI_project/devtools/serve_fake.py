"""用假 LLM 启动真实看板，方便在浏览器里手点审批而不用花 API 钱。

用法：
    python devtools/serve_fake.py                # 默认场景：请求写 workspace/notes/demo.txt
    python devtools/serve_fake.py --port 8010

启动后打开 http://127.0.0.1:<port>/ ，新建任务随便填什么（假模型不看任务内容），
就会看到详情页弹出审批条。点批准后文件会真的写到 workspace/ 下。

想换场景直接改下面的 build_script()。
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 必须赶在 app.tracer / app.tools 被导入之前设置：它们是在模块加载时读这两个变量的。
# 跟踪记录和工作目录都要隔离——只隔离 traces 不够，
# workspace 里的 MEMORY.md / PLAN.md 是后续运行的输入，
# 假演示写进去会被真实 agent 当成自己的记忆读出来。
os.environ.setdefault("TRACES_DIR", str(ROOT / "traces-fake"))
os.environ.setdefault("WORKSPACE_DIR", str(ROOT / "workspace-fake"))

from devtools.fake_llm import FakeClient, call_tool, finish, update_plan  # noqa: E402


def build_compact_script(messages: list) -> list:
    """压缩场景：跑足够多轮，让上下文超过预算从而触发压缩。

    轮数必须够多——上下文压缩是拿"较早的若干条消息"换一份摘要，
    消息本来就少的时候没有可压的东西（见 agent.MIN_COMPACT_MESSAGES）。
    这里用只读工具，所以不会弹审批，跑起来不用管它。
    """
    task = _task_of(messages)
    rounds = [
        call_tool("get_current_time"),
        call_tool("calculator", expression="1+1"),
        call_tool("list_files", path="."),
        call_tool("get_current_time"),
        call_tool("calculator", expression=" 2*3"),
        call_tool("list_files", path="app"),
        call_tool("get_current_time"),
        call_tool("calculator", expression="10/4"),
    ]
    return rounds + [finish(f"（假模型）跑完 {len(rounds)} 轮，任务：{task[:30]}")]


def _task_of(messages: list) -> str:
    return next(
        (m.get("content", "") for m in messages
         if isinstance(m, dict) and m.get("role") == "user"),
        "",
    )


def build_script(messages: list) -> list:
    """默认场景：演示 记忆 → 写文件审批 → 记一笔 → 显式完成。"""
    task = _task_of(messages)
    path = "notes/demo.txt"
    content = (
        "（这是 devtools/serve_fake.py 的演示文件，不是真实任务结果）\n"
        f"你输入的任务：{task}\n"
    )
    steps = ["查看长期记忆", "把内容写入文件（需审批）", "记录本次结论"]
    return [
        # 1. 先看看以前记过什么（跨运行记忆）
        call_tool("recall"),
        # 2. 动手前先规划（规划）
        update_plan(f"处理任务：{task[:40]}", steps, done=0),
        # 3. 请求写文件 → 触发人工审批（人在环）
        call_tool("write_file", path=path, content=content),
        # 4. 完成两步后推进计划
        update_plan(f"处理任务：{task[:40]}", steps, done=2),
        # 5. 把这次的结论记下来，供下次运行使用（跨运行记忆）
        call_tool("remember", note=f"最近一次任务「{task[:40]}」的结果写在 workspace/{path}"),
        # 6. 走 finish 显式声明完成（所以会显示 ended_by = 模型主动声明完成）
        finish(
            f"（假模型，未调用真实 API）已把内容写入 workspace/{path}。\n"
            "要得到真实回答，请用 8000 端口那个真实看板。",
            summary="recall → update_plan → write_file → update_plan → remember → finish",
        ),
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--scenario", choices=["default", "compact"], default="default",
        help="default=记忆与审批演示；compact=长上下文触发压缩演示",
    )
    args = parser.parse_args()

    compact = args.scenario == "compact"
    if compact:
        # 预算和保留条数都调小，否则演示那几轮根本压不到。
        # 必须在 import app.agent 之前设置——它是在模块加载时读的。
        os.environ.setdefault("CONTEXT_BUDGET_TOKENS", "900")
        os.environ.setdefault("CONTEXT_KEEP_RECENT", "3")

    from app import config

    # agent 在调用时读 config.client，替换这里就够了，零生产代码改动。
    # auto_reset 必须开：否则脚本只够跑第一条任务，后面每条都失败。
    script = build_compact_script if compact else build_script
    config.client = FakeClient(
        script,
        auto_reset=True,
        # 压缩场景要按真实消息内容估算 token，否则上下文不会随压缩降下来
        estimate_usage=compact,
    )

    import uvicorn

    from app.tools import WORKSPACE_ROOT
    from app.tracer import TRACES_DIR

    print(f"假 LLM 看板: http://{args.host}:{args.port}")
    print(f"跟踪记录: {TRACES_DIR}")
    print(f"工作目录: {WORKSPACE_ROOT}")
    print("（两者都与真实看板隔离）")
    if args.scenario == "compact":
        print(f"场景: 上下文压缩（预算 {config.CONTEXT_BUDGET_TOKENS} tokens，"
              f"保留最近 {config.CONTEXT_KEEP_RECENT} 条）")
        print("（不调用真实 API；新建任务后会跑 8 轮工具，无需审批，看压缩事件）")
    else:
        print("场景: 记忆 + 人工审批")
        print("（不调用真实 API，随便新建一个任务即可触发审批）")
    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
