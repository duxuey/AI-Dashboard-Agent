"""FastAPI 应用：提供看板页面 + 轮询接口。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config
from .agent import run_agent
from .approval import Decision
from .ingest import router as ingest_router
from .tracer import store

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="AI-Dashboard-Agent")
app.include_router(ingest_router)

# 记录在途的 agent 任务，避免重复启动
_bg_tasks: dict[str, asyncio.Task] = {}


@app.middleware("http")
async def no_cache_for_pages(request, call_next):
    """页面与静态资源禁用浏览器缓存。

    本地开发场景：改完 static 下的文件刷新即可生效，
    否则浏览器会按启发式规则沿用旧副本，看起来像"改动没生效"。
    """
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


class RunRequest(BaseModel):
    task: str


class ApprovalDecisionRequest(BaseModel):
    approved: bool
    reason: Optional[str] = None


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/run")
async def run_page() -> FileResponse:
    """详情页：展示某条 run 的调用路径/日志/上下文。"""
    return FileResponse(STATIC_DIR / "run.html")


@app.get("/stats")
async def stats_page() -> FileResponse:
    """统计页：聚合展示运行统计。"""
    return FileResponse(STATIC_DIR / "stats.html")


@app.get("/settings")
async def settings_page() -> FileResponse:
    """设置页：展示当前配置与关于信息。"""
    return FileResponse(STATIC_DIR / "settings.html")


@app.get("/api/stats")
async def get_stats() -> dict:
    return store.stats()


@app.get("/api/config")
async def get_config() -> dict:
    """返回当前配置（密钥脱敏）。"""
    key = config.API_KEY
    if key:
        preview = key[:4] + "****" + key[-4:] if len(key) > 8 else "****"
    else:
        preview = ""
    return {
        "model": config.MODEL,
        "base_url": config.BASE_URL,
        "host": config.HOST,
        "port": config.PORT,
        "api_key_configured": bool(key),
        "api_key_preview": preview,
    }


@app.get("/api/systems")
async def list_systems() -> dict:
    """已接入的系统注册表（key → 显示名）。

    注册表只负责「给 key 一个人类可读的名字」，不是白名单：
    未注册的 source 上报照收，前端拿原始 key 显示。
    """
    return {
        "systems": [{"key": k, "label": v} for k, v in config.SYSTEMS.items()],
        "default": config.DASHBOARD_SOURCE,
    }


@app.get("/api/runs")
async def list_runs() -> dict:
    return {"runs": store.list_runs()}


@app.post("/api/runs")
async def create_run(req: RunRequest) -> dict:
    task = req.task.strip()
    if not task:
        raise HTTPException(status_code=400, detail="task 不能为空")
    if config.client is None:
        raise HTTPException(status_code=400, detail="未配置 DEEPSEEK_API_KEY")

    # 先建 run 拿到 id，再把 agent 放后台跑
    run = await store.start_run(task, config.MODEL, config.DASHBOARD_SOURCE)
    run_id = run.id
    _bg_tasks[run_id] = asyncio.create_task(_run_and_cleanup(run_id, task))
    return {"run_id": run_id}


async def _run_and_cleanup(run_id: str, task: str) -> None:
    try:
        # 复用上面已建好的 run，run_agent 不再自己 start_run
        await run_agent(task, run_id=run_id)
    except asyncio.CancelledError:
        # run 被删除或进程关停：不落盘、不追加 run_end，直接退出
        raise
    except Exception:
        # run_agent 内部已记录错误；此处兜底避免未处理异常
        await store.finish_run(run_id, error="agent 未处理异常")
    finally:
        _bg_tasks.pop(run_id, None)


@app.delete("/api/runs/{run_id}")
async def delete_run(run_id: str) -> dict:
    """删除一条历史 run（内存 + 落盘文件）。

    必须先取消在途的后台任务：否则 agent 会继续跑，甚至在人点了「批准」之后
    真的执行写操作——删除一条 run 的语义是「停下」，不能反而让它继续。
    取消会让等待审批的 await 抛出 CancelledError，agent 直接退场。
    """
    task = _bg_tasks.pop(run_id, None)
    if task is not None:
        task.cancel()

    deleted = await store.delete_run(run_id)
    store.drop_pending_for_run(run_id)     # 清掉挂起的审批，避免残留条目

    if not deleted:
        raise HTTPException(status_code=404, detail="run 不存在")
    return {"deleted": run_id}


@app.post("/api/runs/{run_id}/approvals/{approval_id}")
async def decide_approval(run_id: str, approval_id: str, req: ApprovalDecisionRequest) -> dict:
    """人工审批：批准或拒绝一次挂起的工具调用。"""
    if store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="run 不存在")

    ok = store.resolve_approval(
        approval_id,
        run_id,
        Decision(
            approved=req.approved,
            reason=(req.reason or "").strip(),
            decided_by="human",
        ),
    )
    if not ok:
        # 已经结束：超时自动拒绝、已被决定、或 run 已被删除
        raise HTTPException(status_code=409, detail="该审批已结束（已决定、已超时或 run 已删除）")
    return {"approval_id": approval_id, "approved": req.approved}


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str) -> dict:
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run 不存在")
    events = store.get_events(run_id)
    return {
        "run": run.to_dict(),
        "events": [e.to_dict() for e in events],
    }


@app.get("/api/runs/{run_id}/events")
async def get_events(run_id: str, after: str | None = None) -> dict:
    """返回事件列表；after 传事件 id 时只返回其后的增量事件（轮询用）。"""
    if store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="run 不存在")
    events = store.get_events(run_id)
    if after:
        events = _events_after(events, after)
    return {"events": [e.to_dict() for e in events], "count": len(events)}


@app.get("/api/runs/{run_id}/events/{event_id}")
async def get_event(run_id: str, event_id: str) -> dict:
    if store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="run 不存在")
    for e in store.get_events(run_id):
        if e.id == event_id:
            return e.to_dict()
    raise HTTPException(status_code=404, detail="事件不存在")


def _events_after(events: list, after: str) -> list:
    for i, e in enumerate(events):
        if e.id == after:
            return events[i + 1 :]
    # after 不在列表中（可能被裁剪/历史），返回全部
    return events


# 静态资源（js/css）
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
