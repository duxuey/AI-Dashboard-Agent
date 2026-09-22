"""外部 ingest 路由：供 canvas 等外部 agent 把调用轨迹上报到看板。

与自建 run（POST /api/runs）区分开：ingest 允许客户端提供 run_id / event id / parent_id，
由外部 agent 自己组织调用路径树，看板只负责存储与展示。

source 标明这条 run 来自哪个系统，取值见 config.SYSTEMS 里的 key（如 "canvas"）。
**不做白名单校验**：新项目接进来时可以先上报、后补显示名，看板会原样展示这个 key。
不传 source 时用 "canvas"，是给老客户端留的兼容默认值。
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .tracer import store

router = APIRouter(prefix="/api/ingest", tags=["ingest"])


class IngestStartRequest(BaseModel):
    run_id: str
    task: str
    model: str = ""
    source: str = "canvas"


class IngestEvent(BaseModel):
    id: Optional[str] = None
    parent_id: Optional[str] = None
    type: str
    name: Optional[str] = None
    ts_ms: Optional[float] = None
    wall_ts: Optional[str] = None
    input: Any = None
    output: Any = None
    meta: dict = {}


class IngestEventsRequest(BaseModel):
    events: list[IngestEvent]


class IngestFinishRequest(BaseModel):
    final_output: Any = None
    error: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    # 怎么结束的，取值见 tracer.ENDED_BY。
    # 不传就按老规矩推断（有 error 记 failed，否则 completed）——
    # 老客户端不受影响，新客户端能如实表达「跑到迭代上限没做完」。
    ended_by: Optional[str] = None


@router.post("/runs")
async def ingest_start(req: IngestStartRequest) -> dict:
    run = await store.ingest_start_run(req.run_id, req.task, req.model, req.source)
    return {"run_id": run.id, "status": run.status}


@router.post("/runs/{run_id}/events")
async def ingest_events(run_id: str, req: IngestEventsRequest) -> dict:
    if store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="run 不存在（请先调用 /api/ingest/runs 创建）")
    events = await store.ingest_append_events(run_id, [e.model_dump() for e in req.events])
    return {"accepted": len(events)}


@router.post("/runs/{run_id}/finish")
async def ingest_finish(run_id: str, req: IngestFinishRequest) -> dict:
    ok = await store.ingest_finish_run(
        run_id,
        final_output=req.final_output,
        error=req.error,
        input_tokens=req.input_tokens,
        output_tokens=req.output_tokens,
        ended_by=req.ended_by,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="run 不存在")
    return {"run_id": run_id, "finished": True}
