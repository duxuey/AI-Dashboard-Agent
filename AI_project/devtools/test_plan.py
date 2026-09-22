"""验证规划：计划落盘、进提示词、事件上树，以及最关键的——跨运行续做。

    python devtools/test_plan.py
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
from app import tools as T  # noqa: E402
from app.agent import _build_system_prompt  # noqa: E402
from app.tools import WORKSPACE_ROOT  # noqa: E402
from devtools.fake_llm import FakeClient, finish, update_plan  # noqa: E402
import app.main as main_mod  # noqa: E402

FAILED = 0


def t(name: str, got, want) -> None:
    global FAILED
    ok = got == want
    if not ok:
        FAILED += 1
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"\n        got ={got!r}\n        want={want!r}"))


def main() -> int:
    shutil.rmtree(WORKSPACE_ROOT, ignore_errors=True)

    print("=== 工具层：写入与格式 ===")
    t("初始没有计划", T.load_plan(), None)

    r = T.update_plan("把三个文件汇总成报告", ["读 a.txt", "读 b.txt", "写报告"], done=0)
    t("写入成功", r["goal"], "把三个文件汇总成报告")
    t("剩余步骤正确", r["remaining"], ["读 a.txt", "读 b.txt", "写报告"])
    t("尚未完成", r["finished"], False)
    t("路径固定", r["path"], "workspace/PLAN.md")

    text = T.PLAN_PATH.read_text(encoding="utf-8")
    t("含目标标题", "# 计划：把三个文件汇总成报告" in text, True)
    t("未完成步骤是空方框", "- [ ] 读 a.txt" in text, True)
    t("落盘文件可被 load_plan 读回", T.load_plan().startswith("# 计划："), True)

    r = T.update_plan("把三个文件汇总成报告", ["读 a.txt", "读 b.txt", "写报告"], done=2)
    text = T.PLAN_PATH.read_text(encoding="utf-8")
    t("已完成的打勾", text.count("- [x]"), 2)
    t("未完成的仍是空框", text.count("- [ ]"), 1)
    t("remaining 只剩一条", r["remaining"], ["写报告"])

    r = T.update_plan("目标", ["一", "二"], done=2)
    t("全部完成时 finished 为真", r["finished"], True)

    print("\n=== 边界 ===")
    t("空 goal 被拒", "不能为空" in T.update_plan("", ["a"]).get("error", ""), True)
    t("空 steps 被拒", "不能为空" in T.update_plan("目标", []).get("error", ""), True)
    t("全是空白步骤等于空", "不能为空" in T.update_plan("目标", ["  ", ""]).get("error", ""), True)
    t("done 越界（大了）被拒",
      "越界" in T.update_plan("目标", ["a", "b"], done=3).get("error", ""), True)
    t("done 越界（负数）被拒",
      "越界" in T.update_plan("目标", ["a", "b"], done=-1).get("error", ""), True)
    t("步骤过多被拒",
      "过多" in T.update_plan("目标", [f"步骤{i}" for i in range(T.MAX_PLAN_STEPS + 1)]).get("error", ""), True)

    import inspect
    t("只接受三个参数（路径锁死）",
      list(inspect.signature(T.update_plan).parameters), ["goal", "steps", "done"])
    t("update_plan 不需要审批", "update_plan" in T.APPROVAL_REQUIRED_TOOLS, False)

    print("\n=== 计划进系统提示词 ===")
    T.PLAN_PATH.unlink(missing_ok=True)
    t("没有计划时不注入", "已有一份计划" not in _build_system_prompt(), True)

    T.update_plan("汇总报告", ["读文件", "写报告"], done=1)
    prompt = _build_system_prompt()
    t("有计划时注入", "已有一份计划" in prompt, True)
    t("注入内容含目标", "汇总报告" in prompt, True)
    t("注入内容含进度", "- [x] 读文件" in prompt, True)
    t("提示可能过时、可覆盖", "忽略它并用 update_plan 覆盖" in prompt, True)

    with TestClient(main_mod.app) as client:
        def run_task(script, task):
            config.client = FakeClient(script)
            rid = client.post("/api/runs", json={"task": task}).json()["run_id"]
            import time
            for _ in range(300):
                r = client.get(f"/api/runs/{rid}").json()["run"]
                if r["status"] not in ("running", "awaiting_approval"):
                    return rid, r
                time.sleep(0.05)
            return rid, client.get(f"/api/runs/{rid}").json()["run"]

        def ev(rid):
            return client.get(f"/api/runs/{rid}/events").json()["events"]

        print("\n=== 计划更新上树 ===")
        T.PLAN_PATH.unlink(missing_ok=True)
        rid, result = run_task(
            [update_plan("三步任务", ["第一步", "第二步", "第三步"], done=0),
             update_plan("三步任务", ["第一步", "第二步", "第三步"], done=2),
             finish("做了前两步")],
            "做三步任务",
        )
        t("run 完成", result["status"], "completed")
        evs = ev(rid)
        calls = [e for e in evs if e["type"] == "tool_call" and e["name"] == "tool_call: update_plan"]
        t("两次计划更新都上树", len(calls), 2)
        t("事件带步骤数", len(calls[1]["input"]["steps"]), 3)
        t("事件带进度", calls[1]["input"]["done"], 2)
        t("有日志便于扫读",
          any("更新计划：三步任务（2/3 步）" in e["name"] for e in evs if e["type"] == "log"), True)

        print("\n=== 关键：跨运行续做 ===")
        # 第 1 次运行：规划了 3 步，只做完 1 步就收工（模拟没做完）
        T.PLAN_PATH.unlink(missing_ok=True)
        rid1, r1 = run_task(
            [update_plan("读十个文件并汇总", ["读文件", "汇总", "写报告"], done=1),
             finish("只做完第一步")],
            "第一次：开始读文件",
        )
        t("第 1 次运行完成", r1["status"], "completed")
        plan_text = T.PLAN_PATH.read_text(encoding="utf-8")
        t("计划留下了进度", "- [x] 读文件" in plan_text, True)
        t("未完成步骤还在", "- [ ] 写报告" in plan_text, True)

        # 第 2 次运行：换一个任务，但系统提示词里应该带着上次的计划
        captured = {}

        class Capturing:
            def __init__(self, script):
                self.inner = FakeClient(script)

            @property
            def chat(self): return self
            @property
            def completions(self): return self

            async def create(self, **kw):
                if kw.get("tools"):
                    captured.setdefault("system", kw["messages"][0]["content"])
                return await self.inner.create(**kw)

        config.client = Capturing([finish("接着上次做")])
        rid2 = client.post("/api/runs", json={"task": "第二次：继续"}).json()["run_id"]
        import time
        for _ in range(300):
            r2 = client.get(f"/api/runs/{rid2}").json()["run"]
            if r2["status"] not in ("running", "awaiting_approval"):
                break
            time.sleep(0.05)

        t("第 2 次运行带着上次的计划启动",
          "已有一份计划" in captured.get("system", ""), True)
        t("计划里的目标传过去了",
          "读十个文件并汇总" in captured.get("system", ""), True)
        t("计划里的进度传过去了",
          "- [x] 读文件" in captured.get("system", ""), True)

    shutil.rmtree(WORKSPACE_ROOT, ignore_errors=True)
    shutil.rmtree(_TEST_TRACES, ignore_errors=True)
    print("\n" + ("全部通过" if FAILED == 0 else f"有 {FAILED} 条失败"))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
