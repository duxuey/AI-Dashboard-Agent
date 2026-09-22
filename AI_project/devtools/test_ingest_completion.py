"""验证 ingest 上报路径的完成度语义：外部 agent 也能如实表达「没做完」。

    python devtools/test_ingest_completion.py
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
os.environ["WORKSPACE_DIR"] = str(_TEST_WORKSPACE)

from fastapi.testclient import TestClient  # noqa: E402

from app.tracer import ENDED_BY, INCOMPLETE_ENDED_BY  # noqa: E402
import app.main as main_mod  # noqa: E402

FAILED = 0


def t(name: str, got, want) -> None:
    global FAILED
    ok = got == want
    if not ok:
        FAILED += 1
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"\n        got ={got!r}\n        want={want!r}"))


def main() -> int:
    with TestClient(main_mod.app) as client:
        def ingest(rid, task="外部任务"):
            client.post("/api/ingest/runs", json={"run_id": rid, "task": task, "model": "m", "source": "canvas"})

        def finish(rid, **kw):
            r = client.post(f"/api/ingest/runs/{rid}/finish", json=kw)
            assert r.status_code == 200, r.text
            return client.get(f"/api/runs/{rid}").json()["run"]

        print("=== 常量集合 ===")
        t("iteration_cap/empty_output 属于未完成", sorted(INCOMPLETE_ENDED_BY),
          ["empty_output", "iteration_cap"])
        t("INCOMPLETE_ENDED_BY 是 ENDED_BY 的子集",
          INCOMPLETE_ENDED_BY <= ENDED_BY, True)

        print("\n=== ended_by 决定状态 ===")
        ingest("r-text")
        run = finish("r-text", final_output="做完了", ended_by="text_response")
        t("text_response → completed", run["status"], "completed")
        t("ended_by 被记下", run["ended_by"], "text_response")

        ingest("r-finish")
        run = finish("r-finish", final_output="完成", ended_by="finish_tool")
        t("finish_tool → completed", run["status"], "completed")

        ingest("r-cap")
        run = finish("r-cap", final_output="做了一半", ended_by="iteration_cap")
        t("iteration_cap → incomplete（不再谎报 completed）", run["status"], "incomplete")
        t("ended_by 被记下", run["ended_by"], "iteration_cap")
        t("没有 error（它没出错，只是没做完）", run["error"], None)

        ingest("r-empty")
        run = finish("r-empty", ended_by="empty_output")
        t("empty_output → incomplete", run["status"], "incomplete")

        print("\n=== error 优先于 ended_by ===")
        ingest("r-both")
        run = finish("r-both", error="炸了", ended_by="text_response")
        t("有 error 就是 failed", run["status"], "failed")
        t("error 被记下", run["error"], "炸了")

        print("\n=== 老客户端不传 ended_by：行为不变 ===")
        ingest("r-old-ok")
        run = finish("r-old-ok", final_output="ok")
        t("不传 → completed", run["status"], "completed")
        t("ended_by 为 None", run["ended_by"], None)

        ingest("r-old-err")
        run = finish("r-old-err", error="出错")
        t("不传且带 error → failed", run["status"], "failed")

        print("\n=== 未知 ended_by 不炸，按 completed 处理 ===")
        ingest("r-weird")
        run = finish("r-weird", final_output="x", ended_by="未来才有的值")
        t("不认识的取值退化为 completed", run["status"], "completed")
        t("但原样记下来，不丢信息", run["ended_by"], "未来才有的值")

        print("\n=== run_end 事件带上结束方式 ===")
        ev = client.get("/api/runs/r-cap/events").json()["events"]
        end = [e for e in ev if e["type"] == "run_end"][0]
        t("run_end 的 meta 有 ended_by", end["meta"]["ended_by"], "iteration_cap")
        t("run_end 的 meta 有 status", end["meta"]["status"], "incomplete")

        print("\n=== 统计与列表 ===")
        sc = client.get("/api/stats").json()["status_counts"]
        t("incomplete 计入统计", sc["incomplete"] >= 2, True)
        runs = client.get("/api/runs").json()["runs"]
        listed = {r["id"]: r["status"] for r in runs}
        t("列表里状态正确", listed.get("r-cap"), "incomplete")

    shutil.rmtree(_TEST_TRACES, ignore_errors=True)
    shutil.rmtree(_TEST_WORKSPACE, ignore_errors=True)
    print("\n" + ("全部通过" if FAILED == 0 else f"有 {FAILED} 条失败"))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
