"""验证「按系统区分 run」：配置解析、source 落库、老记录回填、宽松策略。

    python devtools/test_systems.py
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TEST_TRACES = pathlib.Path(tempfile.mkdtemp(prefix="traces-systems-"))
# 必须在 import app.config / app.tracer 之前设置：它们都在模块加载时读环境变量。
# 这里顺便测 .env 里的 SYSTEMS 覆盖与追加。
os.environ["TRACES_DIR"] = str(_TEST_TRACES)
os.environ["SYSTEMS"] = "dashboard:看板X,canvas:画布Y,foo,bar:Bar：项目"

# ---- 老记录回填用的 fixture ----
# 必须写在 import app.tracer 之前：TraceStore 是在模块加载时扫目录的，
# 导入之后再写文件就赶不上这趟加载了。
# (a) run 里没 source，只有 run_start 事件里有 —— 应该回填成事件里的值
# (b) 两处都没有 —— 应该归到看板自建


def _write_trace(run_id: str, run_extra: dict, start_input: dict) -> None:
    payload = {
        "run": {
            "id": run_id, "task": "老记录", "status": "completed", "model": "m",
            "started_at": "2026-09-01 10:00:00", "finished_at": "2026-09-01 10:00:10",
            "input_tokens": 1, "output_tokens": 1, "final_output": None,
            "error": None, "ended_by": "text_response", **run_extra,
        },
        "events": [
            {"id": "e1", "run_id": run_id, "parent_id": None, "type": "run_start",
             "name": "run_start", "ts_ms": 1.0, "wall_ts": "2026-09-01 10:00:00",
             "input": start_input, "output": None, "meta": {}},
        ],
    }
    (_TEST_TRACES / f"{run_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


_write_trace("old-event-only", {}, {"task": "老记录", "model": "m", "source": "canvas"})
_write_trace("old-no-source", {}, {"task": "老记录", "model": "m"})

from fastapi.testclient import TestClient  # noqa: E402

from app import config  # noqa: E402
from app.tracer import store  # noqa: E402
import app.main as main_mod  # noqa: E402

FAILED = 0


def t(name: str, got, want) -> None:
    global FAILED
    ok = got == want
    if not ok:
        FAILED += 1
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"\n        got ={got!r}\n        want={want!r}"))


def main() -> int:
    print("=== SYSTEMS 解析 ===")
    t("内置项被 .env 覆盖显示名", config.SYSTEMS["dashboard"], "看板X")
    t("canvas 显示名可覆盖", config.SYSTEMS["canvas"], "画布Y")
    t("没有冒号的项退化成 key 当显示名", config.SYSTEMS.get("foo"), "foo")
    t("只按第一个冒号切，显示名里可带全角冒号", config.SYSTEMS.get("bar"), "Bar：项目")
    t("顺序保持声明顺序（决定下拉顺序）", list(config.SYSTEMS), ["dashboard", "canvas", "foo", "bar"])
    t("看板自建常量没被 .env 带跑", config.DASHBOARD_SOURCE, "dashboard")
    t("内置默认里 dashboard 的显示名是「AI-Dashboard-Agent」",
      config.DEFAULT_SYSTEMS["dashboard"], "AI-Dashboard-Agent")

    print("\n=== 老记录回填 ===")
    runs = {r["id"]: r for r in store.list_runs()}
    t("只有 run_start 里有 source → 回填", runs["old-event-only"]["source"], "canvas")
    t("两处都没有 → 归看板自建", runs["old-no-source"]["source"], "dashboard")

    with TestClient(main_mod.app) as client:
        print("\n=== /api/systems ===")
        sysinfo = client.get("/api/systems").json()
        t("接口返回注册表", [s["key"] for s in sysinfo["systems"]], ["dashboard", "canvas", "foo", "bar"])
        t("返回看板自建 key", sysinfo["default"], "dashboard")

        print("\n=== ingest 上报的 source 落库 ===")
        def ingest(rid, **kw):
            body = {"run_id": rid, "task": "外部任务", "model": "m"}
            body.update(kw)
            r = client.post("/api/ingest/runs", json=body)
            assert r.status_code == 200, r.text
            return client.get(f"/api/runs/{rid}").json()["run"]

        t("显式传 source", ingest("s-canvas", source="canvas")["source"], "canvas")
        t("未注册的 source 照收不误", ingest("s-new", source="newproj")["source"], "newproj")
        t("不传 source 仍是 canvas（老客户端兼容）", ingest("s-old")["source"], "canvas")
        t("source 出现在列表接口里",
          any(r["source"] == "newproj" for r in client.get("/api/runs").json()["runs"]), True)

        print("\n=== 统计聚合 ===")
        stats = client.get("/api/stats").json()
        t("有 source_counts", "source_counts" in stats, True)
        t("未注册来源也进统计", stats["source_counts"].get("newproj"), 1)
        t("空 source 的 run 不落进空字符串键", "" not in stats["source_counts"], True)

    print("\n=== 看板自建的 source ===")
    run = asyncio.run(store.start_run("自建任务", "m", config.DASHBOARD_SOURCE))
    t("start_run 记下 source", run.source, "dashboard")
    t("run_start 事件里也带 source",
      store.get_events(run.id)[0].input.get("source"), "dashboard")
    t("to_dict 带 source", run.to_dict()["source"], "dashboard")

    print()
    if FAILED:
        print(f"{FAILED} 项不符")
    else:
        print("全部通过")
    shutil.rmtree(_TEST_TRACES, ignore_errors=True)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
