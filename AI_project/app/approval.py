"""人在环审批：agent 执行有副作用的工具前，先暂停等人点头。

设计要点
--------
审批通道只有 `wait()` 一个方法是可替换的，**注册 future 由 agent 统一负责**。
这样「先注册、再写事件、最后 await」的顺序约束对每个通道都成立，
通道本身不需要关心这个顺序。

三种通道：
- WebChannel  默认。等 HTTP 接口 POST /api/runs/{run_id}/approvals/{approval_id}
- CliChannel  CLI 模式下在终端里问一句
- auto_reject 兜底。没有可用通道时自动拒绝，理由写清楚，模型能看懂

超时（WebChannel）走自动拒绝。人在同一个瞬间点「批准」与超时竞争时，
决定会被丢弃、退化为拒绝——对写操作的闸门来说，失败偏向「不放行」是对的。
"""

from __future__ import annotations

import asyncio
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from . import config


@dataclass
class ApprovalRequest:
    """一次待审批的请求，也是展示给人看的内容。"""

    approval_id: str
    run_id: str
    tool: str
    arguments: dict
    target: str                      # 已解析的绝对路径，展示真实落点而非模型的说法
    target_exists: bool              # 覆盖 vs 新建，审阅时含义不同
    size_bytes: int
    preview: str                     # 内容预览（截断）
    timeout_s: float


@dataclass
class Decision:
    approved: bool
    reason: str = ""
    decided_by: str = "human"        # human / timeout / cli / system


class ApprovalChannel(Protocol):
    """审批通道协议：给定请求与等待中的 future，返回一个决定。"""

    async def wait(self, req: ApprovalRequest, pending: "asyncio.Future") -> Decision: ...


class WebChannel:
    """默认通道：挂起等待 HTTP 接口解除。

    超时后 wait_for 会取消 future，此后接口再 resolve 会拿到 False 并返回 409——
    这是刻意的，让前端知道「这次审批已经结束了」。
    """

    async def wait(self, req: ApprovalRequest, pending: "asyncio.Future") -> Decision:
        try:
            return await asyncio.wait_for(pending, req.timeout_s)
        except asyncio.TimeoutError:
            return Decision(
                approved=False,
                reason=f"{req.timeout_s:.0f} 秒内无人审批，已自动拒绝",
                decided_by="timeout",
            )


class CliChannel:
    """CLI 通道：在终端里问 y/n。

    用 daemon 线程读输入，而不是 run_in_executor：
    默认线程池的线程不是 daemon，且 asyncio.run() 退出时会 join 它们，
    于是 Ctrl+C 期间停在 input() 上的线程会让进程挂住直到用户回车。
    """

    async def wait(self, req: ApprovalRequest, pending: "asyncio.Future") -> Decision:
        if not sys.stdin or not sys.stdin.isatty():
            # 管道 / CI 环境：没有人可以回答，直接拒绝而不是把 EOF 当成决定
            return Decision(False, "当前环境无法交互，已自动拒绝", decided_by="system")

        prompt = (
            f"\n\033[33m[需要审批]\033[0m agent 请求执行 {req.tool}\n"
            f"  目标: {req.target}{'（已存在，将被覆盖）' if req.target_exists else '（新建）'}\n"
            f"  大小: {req.size_bytes} 字节\n"
            f"  预览: {req.preview[:200]}{'…' if len(req.preview) > 200 else ''}\n"
            f"是否允许？[y/N] "
        )
        line = await _read_line(prompt)
        approved = line.strip().lower() in ("y", "yes")
        return Decision(
            approved=approved,
            reason="" if approved else "用户在终端拒绝了该操作",
            decided_by="cli",
        )


async def _read_line(prompt: str) -> str:
    """在 daemon 线程里读一行，结果用 call_soon_threadsafe 送回事件循环。"""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()

    def worker() -> None:
        try:
            sys.stderr.write(prompt)
            sys.stderr.flush()
            line = input()
        except EOFError:
            line = ""            # 视为拒绝
        except Exception:
            line = ""
        try:
            loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(line))
        except RuntimeError:
            pass                 # 事件循环已关闭，没人接了

    threading.Thread(target=worker, daemon=True).start()
    return await fut


async def auto_reject(req: ApprovalRequest, pending: "asyncio.Future") -> Decision:
    """没有可用审批通道时的兜底。"""
    return Decision(False, "当前没有可用的审批通道，已自动拒绝", decided_by="system")


# 未显式指定通道时用哪个
DEFAULT_CHANNEL: ApprovalChannel = WebChannel()


def make_request(
    approval_id: str, run_id: str, tool: str, arguments: dict,
    target: str, target_exists: bool, content: str,
    timeout_s: Optional[float] = None,
) -> ApprovalRequest:
    return ApprovalRequest(
        approval_id=approval_id,
        run_id=run_id,
        tool=tool,
        arguments=arguments,
        target=target,
        target_exists=target_exists,
        size_bytes=len(content.encode("utf-8")),
        preview=content[:4000],
        timeout_s=config.APPROVAL_TIMEOUT_S if timeout_s is None else timeout_s,
    )
