"""可离线跑的假 LLM：用脚本化的固定响应替掉真实 API。

为什么需要它
------------
agent 的行为是概率性的，用它调试审批、重试、记忆这些机制时，
真实模型每次的回答都不一样，很难判断「这次通过是因为我的改动对了，
还是模型恰好配合」。脚本化响应把模型变成一个确定性的搭档。

另外它不花钱、不联网，超时场景也不用真等 5 分钟。

怎么用
------
    from devtools.fake_llm import FakeClient, say, call_tool

    client = FakeClient([
        call_tool("write_file", path="a.txt", content="hi"),   # 第 1 轮：请求写文件
        say("已写入。"),                                        # 第 2 轮：给出最终答复
    ])

`app.agent` 是在**调用时**读 `config.client` 的，所以直接替换该属性即可，
不需要改动任何生产代码。见 devtools/serve_fake.py。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class _Function:
    name: str
    arguments: str          # OpenAI 协议里是 JSON 字符串


@dataclass
class _ToolCall:
    id: str
    function: _Function

    def model_dump(self) -> dict:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.function.name, "arguments": self.function.arguments},
        }


class _Message:
    """伪装成 OpenAI SDK 的 ChatCompletionMessage。"""

    def __init__(self, content: Optional[str] = None, tool_calls: Optional[list] = None):
        self.role = "assistant"
        self.content = content
        self.tool_calls = tool_calls

    def model_dump(self, exclude_none: bool = False) -> dict:
        d: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = [tc.model_dump() for tc in self.tool_calls]
        return d


@dataclass
class _Usage:
    prompt_tokens: int = 100
    completion_tokens: int = 20


@dataclass
class _Choice:
    message: _Message
    finish_reason: str = "stop"


@dataclass
class _Response:
    choices: list
    usage: _Usage = field(default_factory=_Usage)


# ---- 构造响应的便捷函数 ----

def say(text: str) -> _Response:
    """一轮纯文本回复（没有工具调用 → agent 循环就此结束）。"""
    return _Response(choices=[_Choice(_Message(content=text))])


def call_tool(name: str, **arguments) -> _Response:
    """一轮请求调用某个工具。arguments 会自动 JSON 序列化。"""
    return call_tools((name, arguments))


def finish(answer: str, summary: str = "") -> _Response:
    """一轮请求调用 finish，声明任务完成。"""
    return call_tool("finish", answer=answer, summary=summary)


def update_plan(goal: str, steps: list, done: int = 0) -> _Response:
    """一轮请求更新计划。"""
    return call_tool("update_plan", goal=goal, steps=steps, done=done)


def fail(exc: Exception) -> Exception:
    """占位：在脚本里表示"这一轮抛异常"，由 FakeClient 识别并 raise。

    用法：FakeClient([fail(RateLimitError(...)), say("第二次成功")])
    """
    return exc


def call_tools(*calls: tuple) -> _Response:
    """一轮里请求调用多个工具。用法：call_tools(("calculator", {"expression": "1+1"}), ...)"""
    tcs = [
        _ToolCall(
            id=f"call_{i}_{name}",
            function=_Function(name=name, arguments=json.dumps(args, ensure_ascii=False)),
        )
        for i, (name, args) in enumerate(calls)
    ]
    return _Response(choices=[_Choice(_Message(tool_calls=tcs), finish_reason="tool_calls")])


def _estimate_tokens(messages: list) -> int:
    """按消息内容粗略估算 prompt_tokens。

    只用于演示/测试：中文约 1.5 字一个 token、英文约 4 字符一个，
    取 /3 是个折中。重点是它会随消息增减而变化，这一点比数值准确更重要。
    """
    chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)
    return max(1, chars // 3)


class ScriptExhausted(RuntimeError):
    """脚本用完了但 agent 还在要下一轮——通常意味着出现了意料之外的重试。"""


class FakeClient:
    """按脚本顺序返回响应，并记录每次实际收到的调用参数。

    script 可以是一个列表，也可以是一个 callable(messages) -> 列表。
    后者用于「每次运行都要一份全新脚本」的场景——见 auto_reset。

    auto_reset=True 时，检测到新会话（messages 只有一条 user 消息）就重置脚本。
    演示服务必须开这个：脚本若只建一次，第一条任务就会把它用光，
    之后每次新建任务都会因为脚本枯竭而失败。

    `calls` 里存的是每次 create() 的完整 kwargs，测试可以据此断言
    「模型看到的 messages 里到底有没有那条拒绝信息」。
    """

    def __init__(
        self,
        script,
        auto_reset: bool = False,
        estimate_usage: bool = False,
        summary_text: str = "（假模型摘要）此前若干轮的工具调用已完成，结论已保留。",
    ):
        """
        estimate_usage=True 时，上报的 prompt_tokens 由当前 messages 的内容
        估算得出，而不是一个固定值。压缩演示必须开它：只有这样，
        压缩把消息变少之后上报的 token 才会跟着降下来——
        用一个只增不减的计数器会得到"压完了还在涨"的假象。

        summary_text 是上下文压缩时的摘要内容（见 create 里对辅助调用的处理）。
        """
        self._source = script
        self._initial = list(script) if isinstance(script, list) else None
        self._script = list(self._initial) if self._initial is not None else []
        self.auto_reset = auto_reset
        self.estimate_usage = estimate_usage
        self.summary_text = summary_text
        self.calls: list[dict] = []

    def _maybe_reset(self, messages: list) -> None:
        """新会话开始时重新装填脚本。

        判据是「还没有任何 assistant / tool 消息」——不能数消息条数，
        因为系统提示词会让首轮就有多条（system + user）。
        """
        if not self.auto_reset:
            return
        roles = [m.get("role") for m in messages if isinstance(m, dict)]
        fresh = roles.count("assistant") == 0 and roles.count("tool") == 0
        if not fresh:
            return
        if callable(self._source):
            self._script = list(self._source(messages))
        elif self._initial is not None:
            self._script = list(self._initial)

    # 让 `client.chat.completions.create(...)` 这条链能走通
    @property
    def chat(self) -> "FakeClient":
        return self

    @property
    def completions(self) -> "FakeClient":
        return self

    async def create(self, **kwargs) -> _Response:
        # 不带 tools 的调用不是 agent 主循环，而是辅助调用（目前只有上下文压缩
        # 的摘要请求）。必须先于 _maybe_reset 判断：
        # 摘要请求的 messages 只有 system + user，外形和"新会话"一模一样，
        # 放它进去会被当成新一轮任务的开始，把脚本重置掉。
        is_aux = not kwargs.get("tools")

        if not is_aux:
            self._maybe_reset(kwargs.get("messages") or [])
        self.calls.append(kwargs)

        if is_aux:
            return _Response(
                choices=[_Choice(_Message(content=self.summary_text))],
                usage=_Usage(prompt_tokens=50, completion_tokens=20),
            )

        if not self._script:
            raise ScriptExhausted(
                f"脚本已用完，但 agent 又发起了第 {len(self.calls)} 次 LLM 调用。"
                "如果这是审批被拒绝后的重试，说明拒绝信息没能让模型停下来。"
            )
        item = self._script.pop(0)
        if isinstance(item, Exception):   # 脚本里用 fail(...) 放的异常
            raise item
        if self.estimate_usage:
            item.usage.prompt_tokens = _estimate_tokens(kwargs.get("messages") or [])
        return item

    # ---- 断言辅助 ----

    def last_messages(self) -> list:
        """最近一次调用发给模型的 messages。"""
        return self.calls[-1].get("messages", []) if self.calls else []

    def tool_results_sent(self) -> list:
        """所有已回灌给模型的 tool 消息内容（解析成对象）。"""
        out = []
        for call in self.calls:
            for m in call.get("messages", []):
                if m.get("role") == "tool":
                    try:
                        out.append(json.loads(m.get("content", "")))
                    except json.JSONDecodeError:
                        out.append(m.get("content"))
        return out
