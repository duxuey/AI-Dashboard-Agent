"""入口：默认启动 Web 看板，带任务参数时走 CLI 单次运行。

用法：
    python run.py                      # 启动 Web 看板（127.0.0.1:8000）
    python run.py "你的任务"            # CLI 单次运行并打印调用路径树 / 日志 / 最终答案
"""

from __future__ import annotations

import asyncio
import sys

from app import config


def _print_tree(events: list, parent_id: str | None = None, prefix: str = "") -> None:
    """把事件列表按 parent_id 打印成树。"""
    children = [e for e in events if e.parent_id == parent_id]
    for i, e in enumerate(children):
        is_last = i == len(children) - 1
        branch = "└── " if is_last else "├── "
        extra = ""
        if e.type == "llm_response":
            m = e.meta or {}
            extra = f"  [{m.get('latency_ms')}ms, {m.get('input_tokens')}/{m.get('output_tokens')} tok, {m.get('stop_reason')}]"
        elif e.type == "tool_call" and e.input is not None:
            extra = f"  args={e.input}"
        elif e.type == "tool_result" and e.output is not None:
            extra = f"  -> {e.output}"
        print(f"{prefix}{branch}{e.name}{extra}")
        _print_tree(events, e.id, prefix + ("    " if is_last else "│   "))


async def _cli_run(task: str) -> None:
    from app.agent import run_agent
    from app.approval import CliChannel
    from app.tracer import store

    print(f"任务: {task}\n")
    # CLI 下没有 Web 看板可点，审批走终端；非交互环境会自动拒绝
    result = await run_agent(task, channel=CliChannel())
    run_id = result["run_id"]

    print("\n" + "=" * 60)
    print("调用路径")
    print("=" * 60)
    _print_tree(store.get_events(run_id))

    print("\n" + "=" * 60)
    print("日志")
    print("=" * 60)
    for e in store.get_events(run_id):
        if e.type in ("log", "error"):
            level = (e.meta or {}).get("level", "info")
            print(f"  [{e.wall_ts[11:]}] {level}: {e.name}")

    print("\n" + "=" * 60)
    print("最终输出")
    print("=" * 60)
    print(result["final_output"] if result["final_output"] else "(无)")
    if result["error"]:
        print(f"\n错误: {result['error']}")


def main() -> None:
    args = sys.argv[1:]
    if args:
        # CLI 单次运行
        task = " ".join(args)
        try:
            asyncio.run(_cli_run(task))
        except KeyboardInterrupt:
            print("\n已取消")
        return

    # 启动 Web 服务
    if config.client is None:
        print("⚠ 未配置 DEEPSEEK_API_KEY，看板可启动但无法运行任务。")
        print("  请复制 .env.example 为 .env 并填入 DeepSeek 密钥。\n")

    import uvicorn

    print(f"看板已启动: http://{config.HOST}:{config.PORT}")
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, reload=False)


if __name__ == "__main__":
    main()
