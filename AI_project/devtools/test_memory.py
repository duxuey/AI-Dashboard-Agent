"""验证跨运行记忆：上一次运行记下的东西，下一次运行能读到。

    python devtools/test_memory.py
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
from app.tools import WORKSPACE_ROOT  # noqa: E402
from devtools.fake_llm import FakeClient, call_tool, finish  # noqa: E402
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

    print("=== 工具层：写入与读取 ===")
    t("初始 memories 为空", T.recall()["count"], 0)
    t("空记忆有说明文字", T.recall()["note"], "记忆为空")

    r = T.remember("用户偏好用中文回复")
    t("写入成功", r.get("remembered"), "用户偏好用中文回复")
    t("条目数变为 1", r["entries_total"], 1)
    t("返回的是相对路径", r["path"], "workspace/MEMORY.md")

    T.remember("项目根目录是 D:/work/project/AI_project")
    t("第二条写入后计数为 2", T.remember("第三条")["entries_total"], 3)

    t("读取全部", T.recall()["count"], 3)
    t("按关键词过滤", T.recall("中文")["count"], 1)
    t("过滤结果内容正确", "中文" in T.recall("中文")["entries"][0], True)
    t("关键词无匹配时不报错", T.recall("不存在的词")["count"], 0)
    t("无匹配有说明", T.recall("不存在的词")["note"], "没有匹配的记忆")

    print("\n=== 边界 ===")
    t("空 note 被拒", "不能为空" in T.remember("   ").get("error", ""), True)
    t("超长 note 被拒", "过长" in T.remember("x" * (T.MAX_NOTE_CHARS + 1)).get("error", ""), True)
    t("超长 note 未落盘", T.recall()["count"], 3)

    # 关键安全属性：remember 不接受路径参数，无法被用来写任意文件
    import inspect
    params = list(inspect.signature(T.remember).parameters)
    t("remember 只接受 note（无法指定路径）", params, ["note"])
    t("recall 只接受 query", list(inspect.signature(T.recall).parameters), ["query"])

    t("落盘文件在 workspace 内",
      str(T.MEMORY_PATH).startswith(str(WORKSPACE_ROOT)), True)
    t("文件确实存在", T.MEMORY_PATH.exists(), True)
    t("文件内容是可读的 markdown",
      T.MEMORY_PATH.read_text(encoding="utf-8").count("- ["), 3)

    print("\n=== 记忆不需要审批（这是刻意的）===")
    t("remember 不在审批清单", "remember" in T.APPROVAL_REQUIRED_TOOLS, False)
    t("recall 不在审批清单", "recall" in T.APPROVAL_REQUIRED_TOOLS, False)

    # 端到端：第一次运行记下，第二次运行读到
    with TestClient(main_mod.app) as client:
        def run_task(script, task):
            config.client = FakeClient(script)
            rid = client.post("/api/runs", json={"task": task}).json()["run_id"]
            import time
            for _ in range(200):
                r = client.get(f"/api/runs/{rid}").json()["run"]
                if r["status"] not in ("running", "awaiting_approval"):
                    return rid, r
                time.sleep(0.05)
            return rid, client.get(f"/api/runs/{rid}").json()["run"]

        print("\n=== 跨运行：第 1 次运行写入记忆 ===")
        T.MEMORY_PATH.unlink(missing_ok=True)
        rid1, run1 = run_task(
            [call_tool("remember", note="上次把报告写在了 workspace/report.md"),
             finish("记住了")],
            "先记一笔",
        )
        t("第 1 次运行完成", run1["status"], "completed")
        t("记忆已落盘", T.MEMORY_PATH.exists(), True)
        t("落盘内容含所记的话", "workspace/report.md" in T.MEMORY_PATH.read_text(encoding="utf-8"), True)

        ev1 = client.get(f"/api/runs/{rid1}/events").json()["events"]
        # finish 本身也是一次 tool_call，所以这里是两个
        tc = [e for e in ev1 if e["type"] == "tool_call"]
        t("remember 作为 tool_call 上树",
          [e["name"] for e in tc], ["tool_call: remember", "tool_call: finish"])
        t("每次 tool_call 都有对应的 tool_result",
          len([e for e in ev1 if e["type"] == "tool_result"]), 2)
        remember_result = [e for e in ev1 if e["type"] == "tool_result"][0]["output"]
        t("结果里带上了记忆文件路径", remember_result["path"], "workspace/MEMORY.md")

        print("\n=== 跨运行：第 2 次运行读到上次的记忆 ===")
        # 这次脚本里第二个动作是 recall，用它验证「确实读得到」
        rid2, run2 = run_task(
            [call_tool("recall", query="报告"),
             finish("读到了")],
            "上次的报告在哪",
        )
        t("第 2 次运行完成", run2["status"], "completed")
        ev2 = client.get(f"/api/runs/{rid2}/events").json()["events"]
        result = [e for e in ev2 if e["type"] == "tool_result"][0]["output"]
        t("recall 返回了条目", result["count"], 1)
        t("读到的正是上次写的内容", "workspace/report.md" in result["content"], True)

        print("\n=== 无记忆时 recall 不报错 ===")
        T.MEMORY_PATH.unlink(missing_ok=True)
        rid3, run3 = run_task([call_tool("recall"), finish("没记忆")], "空记忆下运行")
        t("运行正常完成", run3["status"], "completed")
        ev3 = client.get(f"/api/runs/{rid3}/events").json()["events"]
        r3 = [e for e in ev3 if e["type"] == "tool_result"][0]["output"]
        t("返回空列表而非报错", r3["count"], 0)
        t("有说明文字", r3["note"], "记忆为空")

    shutil.rmtree(WORKSPACE_ROOT, ignore_errors=True)
    shutil.rmtree(_TEST_TRACES, ignore_errors=True)
    print("\n" + ("全部通过" if FAILED == 0 else f"有 {FAILED} 条失败"))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
