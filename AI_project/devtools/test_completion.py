"""验证「任务完成度」相关行为：finish 工具、ended_by、incomplete 状态、可见重试。

    python devtools/test_completion.py
"""

from __future__ import annotations

import os
import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 必须在导入 app.tracer 之前，测试数据不能落进真实 traces/
_TEST_TRACES = pathlib.Path(tempfile.mkdtemp(prefix="traces-test-"))
_TEST_WORKSPACE = pathlib.Path(tempfile.mkdtemp(prefix="workspace-test-"))
os.environ["TRACES_DIR"] = str(_TEST_TRACES)
# 工作目录也必须隔离：测试会 rmtree(WORKSPACE_ROOT) 清理产物，
# 漏了这行就会删掉真实的 workspace/（MEMORY.md、PLAN.md、agent 产出的文件）
os.environ["WORKSPACE_DIR"] = str(_TEST_WORKSPACE)

from fastapi.testclient import TestClient  # noqa: E402

from app import config  # noqa: E402
from app.agent import MAX_ITERATIONS, SYSTEM_PROMPT  # noqa: E402
from app.tools import WORKSPACE_ROOT  # noqa: E402
from devtools.fake_llm import FakeClient, call_tool, fail, finish, say  # noqa: E402
import app.main as main_mod  # noqa: E402

FAILED = 0


def t(name: str, got, want) -> None:
    global FAILED
    ok = got == want
    if not ok:
        FAILED += 1
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"\n        got ={got!r}\n        want={want!r}"))


def run_once(client: TestClient, script: list, task: str = "测试任务") -> dict:
    config.client = FakeClient(script)
    rid = client.post("/api/runs", json={"task": task}).json()["run_id"]
    import time
    for _ in range(200):
        run = client.get(f"/api/runs/{rid}").json()["run"]
        if run["status"] not in ("running", "awaiting_approval"):
            return run
        time.sleep(0.05)
    return client.get(f"/api/runs/{rid}").json()["run"]


def events(client: TestClient, rid: str) -> list:
    return client.get(f"/api/runs/{rid}/events").json()["events"]


def main() -> int:
    shutil.rmtree(WORKSPACE_ROOT, ignore_errors=True)

    class _Boom(Exception):
        status_code = 400          # 不可重试

    class _RateLimit(Exception):
        status_code = 429          # 可重试

    with TestClient(main_mod.app) as client:
        print("=== 系统提示词要求走 finish ===")
        t("提示词提到 finish", "finish" in SYSTEM_PROMPT, True)
        t("提示词要求完成时调用", "必须调用 finish" in SYSTEM_PROMPT, True)

        print("\n=== 模型调用 finish：显式完成 ===")
        run = run_once(client, [finish("答案是 42", "直接给出结论")])
        t("状态 completed", run["status"], "completed")
        t("ended_by=finish_tool", run["ended_by"], "finish_tool")
        t("final_output 取自 answer", run["final_output"], "答案是 42")
        ev = events(client, run["id"])
        t("有 finish 的 tool_call", [e["name"] for e in ev if e["type"] == "tool_call"], ["tool_call: finish"])
        t("有对应的 tool_result", [e["name"] for e in ev if e["type"] == "tool_result"], ["tool_result: finish"])

        print("\n=== 模型只给文本、不调用 finish ===")
        run = run_once(client, [say("直接回答")])
        t("状态仍是 completed", run["status"], "completed")
        t("ended_by=text_response", run["ended_by"], "text_response")
        t("final_output 是文本", run["final_output"], "直接回答")

        print("\n=== 模型返回空内容 ===")
        run = run_once(client, [say("   ")])
        t("状态 incomplete", run["status"], "incomplete")
        t("ended_by=empty_output", run["ended_by"], "empty_output")
        t("无 final_output", run["final_output"], None)

        print("\n=== 跑满迭代次数（以前会被记为 completed）===")
        # 每轮都调只读工具、永不结束
        script = [call_tool("get_current_time") for _ in range(MAX_ITERATIONS + 2)]
        run = run_once(client, script)
        t(f"状态 incomplete（不再谎报 completed）", run["status"], "incomplete")
        t("ended_by=iteration_cap", run["ended_by"], "iteration_cap")
        t("error 说明了原因", "最大迭代次数" in (run["error"] or ""), True)
        ev = events(client, run["id"])
        t("写入了 error 事件", any(e["type"] == "error" for e in ev), True)
        t("实际只跑了 MAX_ITERATIONS 轮",
          len([e for e in ev if e["type"] == "tool_call"]), MAX_ITERATIONS)

        print("\n=== 不可重试的错误：立即失败，不重试 ===")
        run = run_once(client, [fail(_Boom("参数不对")), say("不该走到这里")])
        t("状态 failed", run["status"], "failed")
        t("ended_by=error", run["ended_by"], "error")
        ev = events(client, run["id"])
        t("没有重试事件", len([e for e in ev if e["type"] == "llm_retry"]), 0)
        t("日志说明了不可重试",
          any("不可重试" in e["name"] for e in ev if e["type"] == "log"), True)

        print("\n=== 可重试的错误：退避后成功，且过程可见 ===")
        run = run_once(client, [
            fail(_RateLimit("限流")),
            fail(_RateLimit("还是限流")),
            finish("重试两次后成功"),
        ])
        t("最终 completed", run["status"], "completed")
        t("ended_by=finish_tool", run["ended_by"], "finish_tool")
        ev = events(client, run["id"])
        retries = [e for e in ev if e["type"] == "llm_retry"]
        t("记录了 2 次重试", len(retries), 2)
        t("重试事件带退避时长", [e["meta"]["delay_s"] for e in retries], [1.0, 2.0])
        t("重试事件带尝试序号", [e["meta"]["attempt"] for e in retries], [1, 2])
        t("日志里有 warn 级别记录",
          len([e for e in ev if e["type"] == "log" and (e["meta"] or {}).get("level") == "warn"]) >= 2, True)

        print("\n=== 可重试但用尽次数：失败并说明 ===")
        run = run_once(client, [fail(_RateLimit("一直限流"))] * 5)
        t("状态 failed", run["status"], "failed")
        ev = events(client, run["id"])
        t("重试了 MAX_LLM_RETRIES-1 次", len([e for e in ev if e["type"] == "llm_retry"]), 2)
        t("日志说明已用尽重试",
          any("已用尽重试次数" in e["name"] for e in ev if e["type"] == "log"), True)

        print("\n=== finish 不需要审批 ===")
        config.client = FakeClient([call_tool("write_file", path="a.txt", content="x")])
        rid = client.post("/api/runs", json={"task": "写文件后完成"}).json()["run_id"]
        import time
        for _ in range(100):
            ev = events(client, rid)
            if any(e["type"] == "approval_request" for e in ev):
                break
            time.sleep(0.05)
        t("write_file 仍要审批", len([e for e in ev if e["type"] == "approval_request"]), 1)

        print("\n=== 统计接口包含新状态 ===")
        sc = client.get("/api/stats").json()["status_counts"]
        t("status_counts 有 incomplete", "incomplete" in sc, True)
        t("status_counts 有 awaiting_approval", "awaiting_approval" in sc, True)
        t("incomplete 计数正确", sc["incomplete"], 2)

    shutil.rmtree(WORKSPACE_ROOT, ignore_errors=True)
    shutil.rmtree(_TEST_TRACES, ignore_errors=True)
    print("\n" + ("全部通过" if FAILED == 0 else f"有 {FAILED} 条失败"))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
