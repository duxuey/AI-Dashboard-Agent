"""Agent 循环：DeepSeek（OpenAI 兼容协议）tool-use 循环，边执行边写观测事件。

调用路径树（通过 parent_id 串联）：
run_start
├── llm_call #1
│   ├── tool_call: calculator ── tool_result
│   └── tool_call: write_file
│       ├── approval_request ── approval_decision   ← 有副作用的工具走人工审批
│       └── tool_result
├── llm_call #2
└── run_end

有副作用的工具（见 tools.APPROVAL_REQUIRED_TOOLS）在执行前会暂停，
等人批准后才真正执行；被拒绝时把拒绝本身作为工具结果回灌给模型——
和工具报错一样，失败也是一种 observation。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any, Optional

from . import config
from .approval import DEFAULT_CHANNEL, ApprovalChannel, Decision, make_request
from .tracer import store, _now_ms
from .tools import (
    APPROVAL_REQUIRED_TOOLS,
    TOOL_DEFINITIONS,
    call_tool,
    load_plan,
    resolve_write_target,
)

MAX_ITERATIONS = 15

# 同一 run 内被拒绝多少次后直接终止。
# 没有这个上限，模型看到「被拒绝」很可能原样再请求一次，
# 最多能烧掉 MAX_ITERATIONS 轮真实 LLM 调用。
MAX_REJECTIONS = 3

# 系统提示词。核心是让模型走 finish 这条显式通道——
# 没有它，「模型不再调用工具」是唯一的结束信号，
# 而模型卡住、被截断、干脆放弃都长得一样。
SYSTEM_PROMPT = (
    "你是一个能调用工具的 AI agent，请一步步完成用户的任务。\n"
    "- 需要信息或需要改变环境时，调用相应工具；一次可以调用多个。\n"
    "- 任务完成时，必须调用 finish 工具声明完成并给出最终回答，"
    "不要仅用一段普通文本结束。\n"
    "- 如果确实做不完，也要调用 finish，在 answer 里说明做到哪一步、卡在哪里。\n"
    "- 你有跨运行的长期记忆。任务可能和以前做过的事有关时，先调用 recall 看看；"
    "产生了以后还会用到的结论、进度、用户偏好时，调用 remember 记下来。\n"
    "  不要把临时的中间步骤记进去——记忆会在每次新任务开始时被重新看到，"
    "记太多无关的东西反而是负担。\n"
    "- 任务需要多步才能完成时，先用 update_plan 列出步骤再动手，"
    "每完成一步就更新一次 done。\n"
    "  这不只是给别人看的：你的迭代次数有限，万一没做完，"
    "计划留在文件里，下次运行能接着做。所以宁可把步骤拆细一点。\n"
    "- 如果这次确实没做完，在 finish 的 answer 里说清楚："
    "完成了哪几步、卡在哪一步、下一步该做什么。"
)


def _build_system_prompt() -> str:
    """拼出系统提示词，并把上一次运行留下的计划带上。

    计划自动注入（而记忆靠 agent 主动 recall）是刻意的区别：
    记忆是一堆零散结论，该由 agent 判断哪条相关；
    而计划就是"当前任务做到哪了"，不知道它的话这次运行就是盲的。
    """
    plan = load_plan()
    if not plan:
        return SYSTEM_PROMPT
    return (
        SYSTEM_PROMPT
        + "\n\n【工作区里已有一份计划】\n"
        + plan
        + "\n\n这份计划可能是上一次运行留下的。如果和当前任务无关，"
        "直接忽略它并用 update_plan 覆盖；如果相关，就接着没做完的步骤继续。"
    )


# ---- LLM 调用的重试 ----
# 关掉了 SDK 的隐式重试（见 config.py 的 max_retries=0），自己实现：
# SDK 重试发生在内部，看板上完全看不到——一次成功的结果背后可能重试过两次，
# 而"跨失败前进"恰恰是这个项目要能观察的东西。
MAX_LLM_RETRIES = 3
RETRY_BASE_DELAY_S = 1.0


def _is_retryable(err: Exception) -> bool:
    """区分「等一下就好」和「再试也没用」。

    分错的代价是不对称的：把永久错误当可重试，只会浪费时间和配额；
    把临时错误当永久错误，一次网络抖动就废掉整个任务。
    """
    status = getattr(err, "status_code", None)
    if isinstance(status, int):
        # 429 限流、>=500 服务端问题：可重试
        # 400/401/403/404/422：请求本身有问题，重试只会重复失败
        return status == 429 or status >= 500
    if isinstance(err, (asyncio.TimeoutError, TimeoutError)):
        return True
    # 连接类错误（openai.APIConnectionError 及其底层原因）
    name = type(err).__name__
    if "Connection" in name or "Timeout" in name or "RateLimit" in name:
        return True
    return False


async def _llm_call(run_id: str, messages: list[dict]) -> Any:
    """调用 LLM，带可见的指数退避重试。"""
    last_err: Exception | None = None
    for attempt in range(1, MAX_LLM_RETRIES + 1):
        try:
            return await config.client.chat.completions.create(
                model=config.MODEL,
                messages=messages,
                tools=TOOL_DEFINITIONS,
                max_tokens=2048,
                extra_body={"thinking": {"type": "disabled"}},
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            last_err = e
            retryable = _is_retryable(e)
            if not retryable or attempt == MAX_LLM_RETRIES:
                await store.log(
                    run_id,
                    f"LLM 调用失败（{type(e).__name__}）"
                    + ("，已用尽重试次数" if retryable else "，该错误不可重试"),
                    level="error",
                )
                break

            delay = RETRY_BASE_DELAY_S * (2 ** (attempt - 1))
            await store.append_event(
                run_id,
                "llm_retry",
                f"llm_retry #{attempt}: {type(e).__name__}",
                output={"error": str(e)[:500]},
                meta={"attempt": attempt, "delay_s": delay, "retryable": True},
            )
            await store.log(
                run_id,
                f"LLM 调用失败（{type(e).__name__}: {str(e)[:120]}），"
                f"{delay:.0f}s 后第 {attempt + 1} 次尝试",
                level="warn",
            )
            await asyncio.sleep(delay)

    raise last_err if last_err else RuntimeError("LLM 调用失败")


# ---- 运行内的上下文管理 ----
# 对话会随着工具结果不断堆积。不管理的话，长任务必然撞上上下文上限，
# 表现为一次莫名其妙的失败——而不是"我装不下了，先做个笔记"。
#
# 压缩策略：把较早的轮次交给模型做成一份结构化摘要，用摘要替换掉原文。
# 为什么不做更省事的"直接丢弃旧消息"：那等于让 agent 忘掉自己读过什么，
# 对"读 10 个文件再汇总"这类任务会静默出错。

# 至少要有这么多条旧消息才值得压缩（摘要自身占 1 条）
MIN_COMPACT_MESSAGES = 2

# 估算增量时用的字符/token 比。中文约 1~1.5 字符一个 token，
# 英文约 4 字符一个，取 1.5 是偏保守的折中（倾向于高估 token）。
CHARS_PER_TOKEN_ESTIMATE = 1.5

# 摘要失败后至少隔几轮再试。不加冷却的话，摘要服务持续不可用时会
# 每轮都白花一次 LLM 调用，一直烧到跑满迭代上限。
COMPACT_FAIL_COOLDOWN = 3

COMPACT_PROMPT = (
    "下面是一段 agent 执行任务的对话记录。请把它压缩成一份摘要，用于替代原文。\n"
    "必须保留：\n"
    "1. 已完成的操作及其关键结果（读到的数据、算出的值、写过的文件）；\n"
    "2. 得到的结论和已排除的方向；\n"
    "3. 尚未完成的事项；\n"
    "4. 后续还需要用到的具体数据（数字、路径、名称要原样保留，不要概括）。\n"
    "不要保留：寒暄、重复的推理过程、失败的尝试细节（结果保留即可）。\n"
    "直接输出摘要正文，不要加任何前缀说明。"
)


def _messages_chars(messages: list[dict]) -> int:
    """消息序列的字符总量，用于估算增量。"""
    return sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)


def _projected_tokens(last_prompt_tokens: int, sent_chars: int, now_chars: int) -> int:
    """估算「下一次请求实际会有多大」。

    last_prompt_tokens 是上一轮请求的精确 token 数，但它只覆盖当时发出去的内容。
    上一轮结束后新追加的 assistant 消息和工具结果不在里面——而工具结果恰恰是
    最大的增量（read_file 单次上限 8000 字符）。只看精确值会系统性低估，
    而且低估的正好是最大的那块。

    这里用「精确值 + 增量估算」：已经量准的部分保持精确，
    只对新增部分做估算。
    """
    growth = max(0, now_chars - sent_chars)
    if not growth:
        return last_prompt_tokens
    # CHARS_PER_TOKEN 取小一点是刻意的：宁可高估 token 数、早一点压缩，
    # 也不要低估到撞上窗口上限——后者是整个任务直接失败。
    return last_prompt_tokens + int(growth / CHARS_PER_TOKEN_ESTIMATE)


def _safe_split_point(messages: list[dict], keep_recent: int) -> int | None:
    """找一个安全的切分点，使「要压缩的旧消息」与「保留的新消息」之间不破坏结构。

    切点必须落在 assistant 消息上：一次 tool_calls 和它对应的若干条 tool 结果
    是一个不可拆的整体，从中间切开会让 API 直接报错。
    返回 None 表示找不到安全切点（消息太少，不必压缩）。
    """
    target = len(messages) - keep_recent
    for i in range(target, 0, -1):
        if messages[i].get("role") == "assistant":
            return i
    return None


async def _summarize(run_id: str, chunk: list[dict]) -> str | None:
    """让模型把一段对话压成摘要。失败返回 None——压缩是优化，不该拖垮整个运行。"""
    try:
        resp = await config.client.chat.completions.create(
            model=config.MODEL,
            messages=[
                {"role": "system", "content": COMPACT_PROMPT},
                {"role": "user", "content": json.dumps(chunk, ensure_ascii=False)[:120000]},
            ],
            max_tokens=1500,
            extra_body={"thinking": {"type": "disabled"}},
        )
        text = (resp.choices[0].message.content or "").strip()
        return text or None
    except asyncio.CancelledError:
        raise
    except Exception as e:
        await store.log(run_id, f"上下文压缩失败（{type(e).__name__}: {e}），本轮跳过", level="warn")
        return None


async def _maybe_compact(
    run_id: str, messages: list[dict], prompt_tokens: int, iteration: int
) -> tuple[list[dict], bool]:
    """上下文接近预算时压缩较早的对话。

    返回 (messages, failed)：failed=True 表示这次尝试了压缩但摘要没成功，
    调用方应据此进入冷却，避免每轮都白花一次 LLM 调用。
    """
    budget = config.CONTEXT_BUDGET_TOKENS
    if prompt_tokens < budget:
        return messages, False

    cut = _safe_split_point(messages, config.CONTEXT_KEEP_RECENT)
    if cut is None:
        return messages, False

    # messages[0] 是 system，不参与压缩，也不进摘要
    old, recent = messages[1:cut], messages[cut:]
    # 摘要自己就要占一条消息，压 1 条等于没省（还白花一次 LLM 调用）。
    # 至少要压到 2 条以上才值得动。
    if len(old) < MIN_COMPACT_MESSAGES:
        return messages, False

    summary = await _summarize(run_id, old)
    if summary is None:
        return messages, True

    # 摘要以 user 消息注入：切点保证 recent[0] 是 assistant，
    # 于是序列是 user → assistant(tool_calls) → tool…，结构合法。
    compacted = (
        [messages[0],
         {"role": "user", "content": "【此前对话的摘要，原文因上下文长度已省略】\n" + summary}]
        + recent
    )

    await store.append_event(
        run_id,
        "context_compacted",
        f"context_compacted @iteration {iteration}",
        output={"summary": summary},
        meta={
            "messages_before": len(messages),
            "messages_after": len(compacted),
            "prompt_tokens_before": prompt_tokens,
            "budget_tokens": budget,
        },
    )
    await store.log(
        run_id,
        f"上下文达 {prompt_tokens} tokens（预算 {budget}），"
        f"已将较早的 {len(old)} 条消息压缩为摘要",
        level="warn",
    )
    return compacted, False


def _rejection_payload(info: dict) -> dict:
    """把拒绝结果整理成回灌给模型的内容。

    必须明说「不要重试」：否则模型看到被拒绝，很可能原样再请求一次。
    """
    return {
        "error": f"{info.get('tool', '该工具')} 未获批准，本次操作已被用户拒绝",
        "reason": info.get("reason", ""),
        "decided_by": info.get("decided_by", ""),
        "approved": False,
        "instruction": "不要重复请求同一个操作；请直接给出最终答复，或改用其它不需要审批的方式。",
    }


async def _request_approval(
    run_id: str, name: str, arguments: dict, tool_call_ev_id: str,
    channel: Optional[ApprovalChannel],
) -> dict:
    """暂停等待人工审批，返回工具结果（批准则真正执行，拒绝则返回拒绝信息）。"""
    # 先校验目标路径。路径本来就不合法的，没必要请人点一次「批准」
    # 再告诉他「路径越界」——直接当成工具错误返回给模型。
    try:
        target = str(resolve_write_target(str(arguments.get("path", ""))))
    except ValueError as e:
        return {"error": str(e)}

    approval_id = uuid.uuid4().hex
    content = str(arguments.get("content", ""))
    req = make_request(
        approval_id=approval_id,
        run_id=run_id,
        tool=name,
        arguments=arguments,
        target=target,
        target_exists=Path(target).exists(),
        content=content,
    )

    # 顺序不可颠倒：先注册 future，再写事件，最后 await。
    # 若先写事件，前端可能已经看到并发来决定，而此时还没有 future 可解除。
    pending = store.register_approval(approval_id, run_id)
    await store.set_status(run_id, "awaiting_approval")
    await store.log(run_id, f"等待人工审批：{name} → {req.target}", level="warn")

    req_ev = None
    try:
        req_ev = await store.append_event(
            run_id,
            "approval_request",
            f"approval_request: {name}",
            parent_id=tool_call_ev_id,
            input={
                "tool": name,
                "arguments": arguments,
                "target": req.target,
                "target_exists": req.target_exists,
                "size_bytes": req.size_bytes,
                "content_preview": req.preview,
            },
            meta={"approval_id": approval_id, "timeout_s": req.timeout_s},
        )

        ch = channel or DEFAULT_CHANNEL
        try:
            decision = await ch.wait(req, pending)
        except asyncio.CancelledError:
            # run 被删除 / 进程关停：不写决策事件，直接退场
            raise
        except Exception as e:      # 通道自身出错，按拒绝处理而不是拖垮整个 run
            decision = Decision(False, f"审批通道异常: {e}", decided_by="system")
    finally:
        store.unregister_approval(approval_id)

    await store.append_event(
        run_id,
        "approval_decision",
        f"approval_decision: {'approved' if decision.approved else 'rejected'}",
        parent_id=req_ev.id if req_ev else tool_call_ev_id,
        # meta 里必须带 approval_id：前端靠它把决策和请求配对，
        # 配不上就不知道审批已经结束，审批条会一直挂着
        meta={"approval_id": approval_id},
        output={
            "approved": decision.approved,
            "reason": decision.reason,
            "decided_by": decision.decided_by,
        },
    )
    await store.set_status(run_id, "running")
    await store.log(
        run_id,
        f"审批结果：{'已批准' if decision.approved else '已拒绝'}"
        f"（{decision.decided_by}）{decision.reason}",
        level="info" if decision.approved else "warn",
    )

    if not decision.approved:
        return {
            "_rejected": True,
            "tool": name,
            "reason": decision.reason,
            "decided_by": decision.decided_by,
        }
    return call_tool(name, arguments)


async def run_agent(
    task: str,
    run_id: str | None = None,
    channel: Optional[ApprovalChannel] = None,
) -> dict:
    """执行一次 agent 运行，返回 {run_id, status, final_output, error}。

    run_id 由调用方预先建好时（Web 端先建 run 拿到 id 再放后台跑），
    这里直接复用，不能再 start_run 一次——否则事件会写到另一条 run 上，
    前端拿到的 id 对应的那条永远是空的。

    channel 指定审批通道；不传则用默认的 Web 通道（等看板上点按钮）。
    CLI 模式应传 CliChannel()。
    """
    if config.client is None:
        raise RuntimeError(
            "未配置 DEEPSEEK_API_KEY。请复制 .env.example 为 .env 并填入 DeepSeek 密钥。"
        )

    if run_id is None:
        # 只有 CLI 单次运行会走到这里，同样是看板自己的任务
        run = await store.start_run(task, config.MODEL, config.DASHBOARD_SOURCE)
        run_id = run.id
    await store.log(run_id, f"启动运行，模型={config.MODEL}，任务: {task}")

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _build_system_prompt()},
        {"role": "user", "content": task},
    ]
    final_output: Any = None
    ended_by: str | None = None
    rejections = 0
    last_prompt_tokens = 0
    sent_chars = 0
    compact_blocked_until = 0     # 摘要失败后的冷却窗口

    try:
        for iteration in range(1, MAX_ITERATIONS + 1):
            await store.log(run_id, f"第 {iteration} 次 LLM 调用")

            # 首轮无历史可压。之后用「精确值 + 增量估算」判断——
            # 只看上一轮的 prompt_tokens 会漏掉上一轮新加的工具结果。
            if last_prompt_tokens:
                if iteration <= compact_blocked_until:
                    await store.log(
                        run_id,
                        f"上下文仍超预算，但上次压缩失败，冷却中"
                        f"（到第 {compact_blocked_until} 轮后重试）",
                        level="warn",
                    )
                else:
                    projected = _projected_tokens(
                        last_prompt_tokens, sent_chars, _messages_chars(messages)
                    )
                    messages, failed = await _maybe_compact(
                        run_id, messages, projected, iteration
                    )
                    if failed:
                        compact_blocked_until = iteration + COMPACT_FAIL_COOLDOWN

            # 记录 llm_call（上下文快照）
            llm_call_ev = await store.append_event(
                run_id,
                "llm_call",
                f"llm_call #{iteration}",
                parent_id=None,
                input=messages,
                meta={"model": config.MODEL, "iteration": iteration},
            )

            # 记下这次实际发出去的规模，下一轮据此算增量
            sent_chars = _messages_chars(messages)

            t0 = _now_ms()
            resp = await _llm_call(run_id, messages)
            latency_ms = round(_now_ms() - t0, 1)

            msg = resp.choices[0].message
            usage = resp.usage
            in_tok = getattr(usage, "prompt_tokens", 0) or 0
            out_tok = getattr(usage, "completion_tokens", 0) or 0
            await store.add_tokens(run_id, in_tok, out_tok)
            last_prompt_tokens = in_tok   # 下一轮据此判断是否需要压缩

            tool_calls = list(getattr(msg, "tool_calls", None) or [])

            # 记录 llm_response
            await store.append_event(
                run_id,
                "llm_response",
                f"llm_response #{iteration}",
                parent_id=llm_call_ev.id,
                output={
                    "content": msg.content,
                    "tool_calls": [tc.model_dump() for tc in tool_calls],
                },
                meta={
                    "input_tokens": in_tok,
                    "output_tokens": out_tok,
                    "latency_ms": latency_ms,
                    "finish_reason": resp.choices[0].finish_reason,
                },
            )

            # 追加 assistant 消息（保留 tool_calls 结构）
            messages.append(msg.model_dump(exclude_none=True))

            if tool_calls:
                finished = False
                for tc in tool_calls:
                    name = tc.function.name
                    try:
                        arguments = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        arguments = {}

                    tool_call_ev = await store.append_event(
                        run_id,
                        "tool_call",
                        f"tool_call: {name}",
                        parent_id=llm_call_ev.id,
                        input=arguments,
                        meta={"tool_call_id": tc.id},
                    )

                    # finish：模型主动声明任务完成。这是唯一的"正常完成"路径
                    if name == "finish":
                        result = call_tool(name, arguments)
                        await store.append_event(
                            run_id, "tool_result", f"tool_result: {name}",
                            parent_id=tool_call_ev.id, output=result,
                        )
                        final_output = result.get("answer") or ""
                        ended_by = "finish_tool"
                        finished = True
                        break

                    # 有副作用的工具：模型请求之后、真正执行之前，插入人工闸门
                    if name in APPROVAL_REQUIRED_TOOLS:
                        result = await _request_approval(
                            run_id, name, arguments, tool_call_ev.id, channel
                        )
                        if result.get("_rejected"):
                            rejections += 1
                            result = _rejection_payload(result)
                    else:
                        result = call_tool(name, arguments)

                    await store.append_event(
                        run_id,
                        "tool_result",
                        f"tool_result: {name}",
                        parent_id=tool_call_ev.id,
                        output=result,
                    )

                    # 计划更新额外写一条日志：它表示"进度推进了"，
                    # 混在普通工具调用里不容易一眼看出来
                    if name == "update_plan" and result.get("steps"):
                        await store.log(
                            run_id,
                            f"更新计划：{result['goal']}"
                            f"（{result['done']}/{len(result['steps'])} 步）",
                        )

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
                if rejections >= MAX_REJECTIONS:
                    raise RuntimeError(
                        f"连续 {rejections} 次工具审批被拒绝，已终止运行"
                    )
                if finished:
                    await store.log(run_id, f"任务完成（模型调用 finish），共 {iteration} 次 LLM 调用")
                    break
                continue

            # 没有工具调用。模型直接给了文本，没走 finish——
            # 按旧行为当作完成，但如实记为 text_response，
            # 看板上能和「明确声明完成」区分开。
            final_output = msg.content or ""
            if not final_output.strip():
                # 既没调工具也没给内容：什么都没做成。
                # 这里必须提前返回，不能只 break——落到末尾那句默认 completed 上，
                # 一条空运行又会被记成"已完成"。
                ended_by = "empty_output"
                reason = f"模型返回了空内容（第 {iteration} 次调用），未产生任何结论"
                await store.log(run_id, reason, level="error")
                await store.append_event(run_id, "error", f"error: {reason}", output=reason)
                await store.finish_run(
                    run_id, final_output=None, error=reason,
                    ended_by=ended_by, status="incomplete",
                )
                return {
                    "run_id": run_id, "status": "incomplete",
                    "final_output": None, "error": reason, "ended_by": ended_by,
                }
            ended_by = "text_response"
            await store.log(run_id, f"完成（模型未调用 finish），共 {iteration} 次 LLM 调用")
            break

        # 循环自然跑完（没 break）时 ended_by 仍是 None。
        # 以前这里会静默地记为 completed——一条什么都没做出来的运行，
        # 在看板上显示成绿色"已完成"。
        if ended_by is None:
            ended_by = "iteration_cap"
            reason = f"达到最大迭代次数（{MAX_ITERATIONS}）仍未得出结论"
            await store.log(run_id, reason, level="error")
            await store.append_event(run_id, "error", f"error: {reason}", output=reason)
            await store.finish_run(
                run_id, final_output=None, error=reason,
                ended_by=ended_by, status="incomplete",
            )
            return {
                "run_id": run_id, "status": "incomplete",
                "final_output": None, "error": reason, "ended_by": ended_by,
            }

    except Exception as e:
        await store.log(run_id, f"运行出错: {e}", level="error")
        await store.append_event(run_id, "error", f"error: {e}", output=str(e))
        await store.finish_run(
            run_id, final_output=final_output, error=str(e),
            ended_by="error", status="failed",
        )
        return {
            "run_id": run_id, "status": "failed",
            "final_output": final_output, "error": str(e), "ended_by": "error",
        }

    # 走到这里只有两种：finish_tool（主动声明完成）或 text_response（直接给了回答）。
    # 两者都有产出，状态都算 completed；区别记在 ended_by 上——
    # 状态回答「有没有结果」，ended_by 回答「怎么结束的」，这是两件事。
    # 没有产出的两种情况（空回复、跑满迭代）在上面的分支里已经提前返回 incomplete。
    await store.finish_run(
        run_id, final_output=final_output, ended_by=ended_by, status="completed",
    )
    return {
        "run_id": run_id, "status": "completed",
        "final_output": final_output, "error": None, "ended_by": ended_by,
    }
