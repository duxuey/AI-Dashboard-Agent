"""验证运行内的上下文压缩。

    python devtools/test_context.py
"""

from __future__ import annotations

import os
import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TEST_TRACES = pathlib.Path(tempfile.mkdtemp(prefix="traces-test-"))
_TEST_WORKSPACE = pathlib.Path(tempfile.mkdtemp(prefix="workspace-test-"))
os.environ["TRACES_DIR"] = str(_TEST_TRACES)
# 工作目录也必须隔离：测试会 rmtree(WORKSPACE_ROOT) 清理产物，
# 漏了这行就会删掉真实的 workspace/（MEMORY.md、PLAN.md、agent 产出的文件）
os.environ["WORKSPACE_DIR"] = str(_TEST_WORKSPACE)

from fastapi.testclient import TestClient  # noqa: E402

from app import config  # noqa: E402
from app.agent import (  # noqa: E402
    COMPACT_FAIL_COOLDOWN,
    CHARS_PER_TOKEN_ESTIMATE,
    _messages_chars,
    _projected_tokens,
    _safe_split_point,
)
from app.tools import WORKSPACE_ROOT  # noqa: E402
from devtools.fake_llm import FakeClient, _Response, _Choice, _Message, _Usage, call_tool, finish, say  # noqa: E402
import app.main as main_mod  # noqa: E402

FAILED = 0


def t(name: str, got, want) -> None:
    global FAILED
    ok = got == want
    if not ok:
        FAILED += 1
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"\n        got ={got!r}\n        want={want!r}"))


def usage(tokens: int) -> _Response:
    """构造一个带指定 prompt_tokens 的响应（用来模拟上下文膨胀）。"""
    return _Response(choices=[_Choice(_Message(content="ok"))], usage=_Usage(prompt_tokens=tokens, completion_tokens=5))


class SummaryClient:
    """区分「主对话调用」和「摘要调用」，并记录摘要被要求压缩了什么。"""

    def __init__(self, main_script: list, summary_text: str = "【摘要】已读取 a.txt 和 b.txt"):
        self.main = main_script
        self.summary_text = summary_text
        self.summary_requests: list[str] = []
        self.main_calls: list[dict] = []

    @property
    def chat(self): return self
    @property
    def completions(self): return self

    async def create(self, **kw):
        msgs = kw.get("messages") or []
        if not kw.get("tools"):
            # 没有 tools 参数 = 摘要调用
            self.summary_requests.append(msgs[-1].get("content", ""))
            return _Response(choices=[_Choice(_Message(content=self.summary_text))], usage=_Usage(10, 10))
        self.main_calls.append(kw)
        if not self.main:
            raise RuntimeError("主脚本已用完")
        return self.main.pop(0)


def main() -> int:
    shutil.rmtree(WORKSPACE_ROOT, ignore_errors=True)

    print("=== 预算从模型窗口倒推 ===")
    t("窗口可配置", config.MODEL_CONTEXT_WINDOW, 1_000_000)
    t("预算 = 窗口 × 比例",
      config.CONTEXT_BUDGET_TOKENS,
      int(config.MODEL_CONTEXT_WINDOW * config.CONTEXT_BUDGET_RATIO))
    t("比例留了余量（不贴着窗口）", config.CONTEXT_BUDGET_RATIO < 0.8, True)

    print("\n=== 增量估算：只对新增部分估算，已量准的部分保持精确 ===")
    t("没有新增时等于精确值", _projected_tokens(1000, 500, 500), 1000)
    t("消息变短时不产生负增长（压缩之后会这样）",
      _projected_tokens(1000, 900, 300), 1000)
    # 新增 600 字符 → 600/1.5 = 400 token
    t("新增部分按字符估算",
      _projected_tokens(1000, 500, 1100), 1000 + int(600 / CHARS_PER_TOKEN_ESTIMATE))
    t("估算方向偏保守（宁可高估 token）",
      CHARS_PER_TOKEN_ESTIMATE < 4, True)

    print("\n=== 关键：上一轮刚加了大工具结果时不会被漏掉 ===")
    # 上一轮请求时报 1000 token；之后追加了一条 8000 字符的工具结果
    big_tool = [{"role": "tool", "content": "x" * 8000}]
    added = _messages_chars(big_tool)
    projected = _projected_tokens(1000, 500, 500 + added)
    t("增量被算进去了", projected > 1000, True)
    t("增量规模合理（约 8000/1.5）",
      abs((projected - 1000) - added / CHARS_PER_TOKEN_ESTIMATE) < 2, True)
    t("只看精确值会严重低估",
      projected - 1000 > 5000, True)

    print("\n=== 切分点必须落在 assistant 上 ===")
    msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "tool_calls": [{"id": "1"}]},   # idx 2
        {"role": "tool", "content": "r1"},
        {"role": "tool", "content": "r2"},
        {"role": "assistant", "tool_calls": [{"id": "2"}]},   # idx 5
        {"role": "tool", "content": "r3"},
        {"role": "assistant", "content": "done"},
    ]
    sp = _safe_split_point(msgs, 3)      # 保留最近 3 条 → target=5
    t("切点落在 assistant 上", msgs[sp]["role"], "assistant")
    t("切点不会拆散 tool 序列", sp, 5)

    short = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}, {"role": "assistant", "content": "a"}]
    t("消息太少时不压缩", _safe_split_point(short, 6), None)

    with TestClient(main_mod.app) as client:
        def run_task(script_client, task="测试"):
            config.client = script_client
            rid = client.post("/api/runs", json={"task": task}).json()["run_id"]
            import time
            for _ in range(200):
                r = client.get(f"/api/runs/{rid}").json()["run"]
                if r["status"] not in ("running", "awaiting_approval"):
                    return rid, r
                time.sleep(0.05)
            return rid, client.get(f"/api/runs/{rid}").json()["run"]

        def ev(rid):
            return client.get(f"/api/runs/{rid}/events").json()["events"]

        print("\n=== 上下文在预算内：不压缩 ===")
        original_budget = config.CONTEXT_BUDGET_TOKENS
        config.CONTEXT_BUDGET_TOKENS = 100000
        try:
            c = SummaryClient([call_tool("get_current_time"), finish("好了")])
            rid, result = run_task(c)
            t("正常完成", result["status"], "completed")
            t("没有压缩事件", len([e for e in ev(rid) if e["type"] == "context_compacted"]), 0)
            t("没有发起摘要调用", len(c.summary_requests), 0)

            print("\n=== 上下文超预算：触发压缩 ===")
            config.CONTEXT_BUDGET_TOKENS = 100        # 造一个必然超标的预算
            # 轮次要多一些：旧消息太少时压缩不划算，会被下限挡掉
            c = SummaryClient(
                [call_tool("get_current_time") for _ in range(8)] + [finish("做完了")],
                summary_text="【摘要】已取过 8 次时间",
            )
            rid, result = run_task(c)

            t("run 正常完成", result["status"], "completed")
            evs = ev(rid)
            comp = [e for e in evs if e["type"] == "context_compacted"]
            t("产生了压缩事件", len(comp) >= 1, True)
            t("发起了摘要调用", len(c.summary_requests) >= 1, True)

            first = comp[0]
            # 假模型每轮固定报 100，所以 >100 说明增量被算进来了。
            # 这正是修掉的那个偏差：以前只记 last_prompt_tokens，会漏掉
            # 上一轮新加的工具结果。
            t("压缩时记的是投影值而非原始值",
              first["meta"]["prompt_tokens_before"] > 100, True)
            t("压缩后消息条数不超过压缩前",
              first["meta"]["messages_after"] <= first["meta"]["messages_before"], True)
            t("摘要内容被保留", "已取过 8 次时间" in first["output"]["summary"], True)

            # 真正该省的是内容体积，不是消息条数——
            # 一条 8000 字的工具结果换成一句摘要，条数没变但 token 掉很多
            def content_len(msgs):
                return sum(len(str(m.get("content") or "")) for m in msgs)

            before_len = content_len(c.main_calls[-3]["messages"]) if len(c.main_calls) >= 3 else None
            t("压缩后内容体积显著下降",
              content_len(c.main_calls[-1]["messages"]) < content_len(c.main_calls[0]["messages"]), True)

            # 压缩后，下一次发给模型的 messages 里应含摘要
            last_msgs = c.main_calls[-1]["messages"]
            t("后续调用带上了摘要",
              any("此前对话的摘要" in (m.get("content") or "") for m in last_msgs if m.get("role") == "user"), True)
            t("system 消息仍在首位", last_msgs[0]["role"], "system")
            t("压缩后对象仍是合法序列（无孤立 tool）",
              not any(last_msgs[i]["role"] == "tool" and last_msgs[i-1]["role"] != "assistant"
                      for i in range(1, len(last_msgs))), True)

            print("\n=== 摘要调用失败：不影响主流程 ===")
            class BrokenSummary(SummaryClient):
                async def create(self, **kw):
                    if not kw.get("tools"):
                        raise RuntimeError("摘要服务挂了")
                    return await super().create(**kw)

            c = BrokenSummary(
                [call_tool("get_current_time") for _ in range(10)] + [finish("照样完成")]
            )
            rid, result = run_task(c)
            t("run 仍然完成", result["status"], "completed")
            t("没有产生压缩事件", len([e for e in ev(rid) if e["type"] == "context_compacted"]), 0)

            evs = ev(rid)
            t("日志记录了压缩失败",
              any("上下文压缩失败" in e["name"] for e in evs if e["type"] == "log"), True)

            # 冷却：失败后不该每轮都重试
            fail_logs = [e for e in evs if e["type"] == "log" and "上下文压缩失败" in e["name"]]
            t("失败次数被冷却限制住（不再每轮重试）",
              len(fail_logs) <= 3, True)
            t("确实尝试过压缩", len(fail_logs) >= 1, True)
            cooldown_logs = [e for e in evs if e["type"] == "log" and "冷却中" in e["name"]]
            t("日志说明进入了冷却", len(cooldown_logs) >= 1, True)
            # 10 轮里失败后被跳过若干轮，所以"被跳过"的轮数应该明显多于尝试次数
            t("跳过轮数多于尝试次数（冷却确实在起作用）",
              len(cooldown_logs) > len(fail_logs), True)
            t("冷却窗口与配置相符（跳过的轮数 ≈ 尝试次数 × 冷却）",
              abs(len(cooldown_logs) - len(fail_logs) * COMPACT_FAIL_COOLDOWN) <= COMPACT_FAIL_COOLDOWN,
              True)
        finally:
            config.CONTEXT_BUDGET_TOKENS = original_budget

    shutil.rmtree(WORKSPACE_ROOT, ignore_errors=True)
    shutil.rmtree(_TEST_TRACES, ignore_errors=True)
    print("\n" + ("全部通过" if FAILED == 0 else f"有 {FAILED} 条失败"))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
