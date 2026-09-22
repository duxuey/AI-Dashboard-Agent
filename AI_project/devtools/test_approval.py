"""审批功能的离线端到端测试：跑真实的 FastAPI 应用 + 假 LLM，不花 API 钱。

    python devtools/test_approval.py

覆盖：批准 / 拒绝 / 超时 / 重复提交 / 超时后补点 / 暂停中删除 /
      路径越界 / 孤儿 run 回归 / 只读工具不受影响 / 拒绝次数上限。
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 必须在导入 app.tracer 之前设置：测试产生的记录绝不能落进真实 traces/。
# 之前没隔离，测试跑完往真实数据目录塞了 20+ 条记录，看板上一片「写个文件」。
_TEST_TRACES = pathlib.Path(tempfile.mkdtemp(prefix="traces-test-"))
_TEST_WORKSPACE = pathlib.Path(tempfile.mkdtemp(prefix="workspace-test-"))
os.environ["TRACES_DIR"] = str(_TEST_TRACES)
# 工作目录也必须隔离：测试会 rmtree(WORKSPACE_ROOT) 清理产物，
# 漏了这行就会删掉真实的 workspace/（MEMORY.md、PLAN.md、agent 产出的文件）
os.environ["WORKSPACE_DIR"] = str(_TEST_WORKSPACE)

from fastapi.testclient import TestClient  # noqa: E402

from app import config  # noqa: E402
from app.tracer import store  # noqa: E402
from app.tools import WORKSPACE_ROOT  # noqa: E402
from devtools.fake_llm import FakeClient, ScriptExhausted, call_tool, say  # noqa: E402
import app.main as main_mod  # noqa: E402

FAILED = 0


def t(name: str, got, want) -> None:
    global FAILED
    ok = got == want
    if not ok:
        FAILED += 1
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"\n        got ={got!r}\n        want={want!r}"))


def events(client: TestClient, run_id: str) -> list:
    r = client.get(f"/api/runs/{run_id}/events")
    return r.json()["events"] if r.status_code == 200 else []


def types_of(evs: list) -> list:
    return [e["type"] for e in evs]


def find(evs: list, etype: str) -> list:
    return [e for e in evs if e["type"] == etype]


def pending_ids(evs: list) -> list:
    """尚未有对应决策的 approval_request 的 id。

    只按「有没有 approval_request」判断是不够的——已决定的请求也还在列表里，
    会让人误以为又暂停了一次，进而对着旧的 approval_id 重复提交。
    """
    decided = {e["meta"]["approval_id"] for e in find(evs, "approval_decision")}
    return [
        e["meta"]["approval_id"] for e in find(evs, "approval_request")
        if e["meta"]["approval_id"] not in decided
    ]


def wait_paused(client: TestClient, run_id: str, timeout: float = 5.0) -> bool:
    """轮询直到出现一个「尚未被决定」的审批请求，或 run 已结束。"""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        evs = events(client, run_id)
        if pending_ids(evs):
            return True
        if types_of(evs).count("run_end"):
            return False
        time.sleep(0.05)
    return False


def approve_id(evs: list) -> str:
    return pending_ids(evs)[0]


def setup(client: TestClient, script: list, task: str = "写个文件") -> str:
    config.client = FakeClient(script)
    r = client.post("/api/runs", json={"task": task})
    assert r.status_code == 200, r.text
    return r.json()["run_id"]


def drain(client: TestClient, run_id: str, timeout: float = 5.0) -> None:
    """等后台任务跑完，避免用例之间互相干扰。"""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/runs/{run_id}")
        if r.status_code != 200 or r.json()["run"]["status"] in ("completed", "failed"):
            return
        time.sleep(0.05)


def body(client: TestClient) -> None:
    target = WORKSPACE_ROOT / "notes" / "demo.txt"

    print("=== 孤儿 run 回归（Phase 0）===")
    rid = setup(client, [say("你好")], task="回归检查")
    drain(client, rid)
    evs = events(client, rid)
    t("返回的 id 上确有事件（修复前为空）", len(evs) >= 3, True)
    t("含 run_start", "run_start" in types_of(evs), True)
    t("含 run_end", "run_end" in types_of(evs), True)

    print("\n=== 批准 ===")
    rid = setup(client, [
        call_tool("write_file", path="notes/demo.txt", content="hello\n"),
        say("写好了。"),
    ])
    t("出现了审批请求", wait_paused(client, rid), True)
    evs = events(client, rid)
    req = find(evs, "approval_request")[0]
    t("审批请求挂在 tool_call 下", req["parent_id"], find(evs, "tool_call")[0]["id"])
    t("此时 run 为 awaiting_approval", client.get(f"/api/runs/{rid}").json()["run"]["status"], "awaiting_approval")
    t("未批准前文件不存在", target.exists(), False)

    r = client.post(f"/api/runs/{rid}/approvals/{approve_id(evs)}", json={"approved": True})
    t("批准返回 200", r.status_code, 200)
    drain(client, rid)
    t("批准后文件已写入", target.exists(), True)
    t("文件内容正确", target.read_text(encoding="utf-8"), "hello\n")
    evs = events(client, rid)
    dec = find(evs, "approval_decision")[0]
    t("决策由 human 做出", dec["output"]["decided_by"], "human")
    t("决策为 approved", dec["output"]["approved"], True)
    t("决策挂在请求下", dec["parent_id"], req["id"])
    t("run 回到 completed", client.get(f"/api/runs/{rid}").json()["run"]["status"], "completed")
    t("tool_result 是 tool_call 的兄弟（不在 request 下）",
      [e["parent_id"] for e in find(evs, "tool_result")], [find(evs, "tool_call")[0]["id"]])
    t("审批结束后无残留挂起", store.pending_for_run(rid), None)

    print("\n=== 拒绝 ===")
    target.unlink(missing_ok=True)
    rid = setup(client, [
        call_tool("write_file", path="notes/demo.txt", content="不该写进去\n"),
        say("好的，我不写了。"),
    ])
    t("出现审批请求", wait_paused(client, rid), True)
    evs = events(client, rid)
    r = client.post(f"/api/runs/{rid}/approvals/{approve_id(evs)}", json={"approved": False, "reason": "不需要"})
    t("拒绝返回 200", r.status_code, 200)
    drain(client, rid)
    t("拒绝后文件未创建", target.exists(), False)
    evs = events(client, rid)
    dec = find(evs, "approval_decision")[0]
    t("decided_by=human", dec["output"]["decided_by"], "human")
    t("approved=False", dec["output"]["approved"], False)
    t("拒绝理由已记录", dec["output"]["reason"], "不需要")
    # 关键：模型必须被告知「不要重试」
    sent = config.client.tool_results_sent()
    t("拒绝信息已回灌给模型", any(isinstance(s, dict) and s.get("approved") is False for s in sent), True)
    t("回灌内容含「不要重试」指令",
      any(isinstance(s, dict) and "不要重复请求" in s.get("instruction", "") for s in sent), True)
    t("run 正常结束而非失败", client.get(f"/api/runs/{rid}").json()["run"]["status"], "completed")

    print("\n=== 重复提交 / 超时后补点 ===")
    target.unlink(missing_ok=True)
    rid = setup(client, [
        call_tool("write_file", path="notes/demo.txt", content="x\n"),
        say("done"),
    ])
    wait_paused(client, rid)
    evs = events(client, rid)
    aid = approve_id(evs)
    t("首次批准 200", client.post(f"/api/runs/{rid}/approvals/{aid}", json={"approved": True}).status_code, 200)
    t("二次提交 409", client.post(f"/api/runs/{rid}/approvals/{aid}", json={"approved": False}).status_code, 409)
    drain(client, rid)
    t("决策事件有且仅有一条", len(find(events(client, rid), "approval_decision")), 1)

    print("\n=== 超时自动拒绝 ===")
    target.unlink(missing_ok=True)
    original = config.APPROVAL_TIMEOUT_S
    config.APPROVAL_TIMEOUT_S = 1.0
    try:
        rid = setup(client, [
            call_tool("write_file", path="notes/demo.txt", content="超时\n"),
            say("算了"),
        ])
        wait_paused(client, rid)
        evs = events(client, rid)
        aid = approve_id(evs)
        drain(client, rid, timeout=8)
        dec = find(events(client, rid), "approval_decision")[0]
        t("超时后自动拒绝", dec["output"]["approved"], False)
        t("decided_by=timeout", dec["output"]["decided_by"], "timeout")
        t("超时后文件未创建", target.exists(), False)
        t("超时后补点返回 409",
          client.post(f"/api/runs/{rid}/approvals/{aid}", json={"approved": True}).status_code, 409)
    finally:
        config.APPROVAL_TIMEOUT_S = original

    print("\n=== 暂停中删除 ===")
    target.unlink(missing_ok=True)
    rid = setup(client, [
        call_tool("write_file", path="notes/demo.txt", content="删除测试\n"),
        say("done"),
    ])
    wait_paused(client, rid)
    evs = events(client, rid)
    aid = approve_id(evs)
    t("删除返回 200", client.delete(f"/api/runs/{rid}").status_code, 200)
    import time
    stats_after = client.get("/api/stats").json()["total_tool_calls"]
    time.sleep(0.4)     # 给可能残留的后台任务足够时间写事件
    t("删除后文件未创建", target.exists(), False)
    t("删除后 run 不存在", client.get(f"/api/runs/{rid}").status_code, 404)
    t("删除后补点审批 404", client.post(f"/api/runs/{rid}/approvals/{aid}", json={"approved": True}).status_code, 404)
    t("残留挂起审批已清理", store.pending_for_run(rid), None)
    t("后台任务已从 _bg_tasks 移除", rid in main_mod._bg_tasks, False)
    # 关键：取消成功后后台任务不再写事件，统计不会随时间增长
    t("统计未被幽灵事件推高", client.get("/api/stats").json()["total_tool_calls"], stats_after)

    print("\n=== 路径越界 ===")
    rid = setup(client, [
        call_tool("write_file", path="../../evil.txt", content="逃逸\n"),
        say("被挡住了。"),
    ])
    drain(client, rid)
    evs = events(client, rid)
    t("越界不弹审批条", len(find(evs, "approval_request")), 0)
    tr = find(evs, "tool_result")[0]["output"]
    t("越界以工具错误返回", "越界" in tr.get("error", ""), True)
    t("未在项目根写入文件", (WORKSPACE_ROOT.parent / "evil.txt").exists(), False)
    t("run 正常结束", client.get(f"/api/runs/{rid}").json()["run"]["status"], "completed")

    print("\n=== 只读工具不受影响 ===")
    rid = setup(client, [
        call_tool("calculator", expression="(34*12+8)/8"),
        say("算完了"),
    ])
    drain(client, rid)
    evs = events(client, rid)
    t("只读工具无审批请求", len(find(evs, "approval_request")), 0)
    t("只读工具直接执行", find(evs, "tool_result")[0]["output"].get("result"), 52)
    t("run completed", client.get(f"/api/runs/{rid}").json()["run"]["status"], "completed")

    print("\n=== 拒绝次数上限 ===")
    target.unlink(missing_ok=True)
    script = [call_tool("write_file", path="notes/demo.txt", content="反复\n") for _ in range(6)]
    rid = setup(client, script + [say("放弃")])
    for _ in range(3):
        if not wait_paused(client, rid):
            break
        evs = events(client, rid)
        client.post(f"/api/runs/{rid}/approvals/{approve_id(evs)}", json={"approved": False, "reason": "no"})
    drain(client, rid, timeout=8)
    evs = events(client, rid)
    t("拒绝满 3 次后终止为 failed", client.get(f"/api/runs/{rid}").json()["run"]["status"], "failed")
    t("没有继续请求第 4 次", len(find(evs, "approval_request")), 3)
    t("文件始终未创建", target.exists(), False)

    print()


def main() -> int:
    shutil.rmtree(WORKSPACE_ROOT, ignore_errors=True)
    # 必须用上下文管理器：否则每个请求会跑在各自的 event loop 里，
    # 请求一结束循环就被拆掉，等待审批的 future 会被取消，
    # 审批注册表随即清空——测试会误报成产品缺陷。
    with TestClient(main_mod.app) as client:
        body(client)
    shutil.rmtree(WORKSPACE_ROOT, ignore_errors=True)
    shutil.rmtree(_TEST_TRACES, ignore_errors=True)   # 测试数据用完即弃
    print("全部通过" if FAILED == 0 else f"有 {FAILED} 条失败")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
