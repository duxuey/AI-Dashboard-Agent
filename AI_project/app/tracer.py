"""观测层：记录 agent 运行过程中的所有事件，形成调用路径树。

核心概念：
- TraceEvent  单条事件（含 id / parent_id 形成树形链路）
- Run         一次 agent 运行（任务、状态、累计 token、最终输出）
- TraceStore  内存中的事件/运行仓库，负责落盘与历史加载

事件类型（type）：
- run_start     根节点，标记一次运行开始
- run_end       根节点末尾，标记运行结束
- llm_call      一次 LLM 调用（input 为发给模型的 messages 快照）
- llm_response  LLM 返回（meta 含 tokens / latency / stop_reason）
- tool_call     一次工具调用（name + input 为入参）
- tool_result   工具返回（output 为结果）
- log           普通日志（level 在 meta 中）
- error         错误事件
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional


# 落盘目录。可用 TRACES_DIR 环境变量覆盖——测试台和假 LLM 服务靠它
# 把自己写的数据隔离出去，否则会混进真实数据里（保存 / 加载是同一份目录）。
_env_traces = os.getenv("TRACES_DIR")
TRACES_DIR = (
    Path(_env_traces).resolve() if _env_traces
    else Path(__file__).resolve().parent.parent / "traces"
)

# run 的状态取值。running 与 awaiting_approval 是「存活态」——还在跑；
# 其余三个是终态，一旦进入就不可再变。
RUN_STATUSES = frozenset({"running", "awaiting_approval", "completed", "failed", "incomplete"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "incomplete"})

# run 是怎么结束的。把「模型不再调用工具」和「模型明确宣布完成」区分开——
# 前者只是个副作用，模型卡住、放弃、被截断都会表现为它，
# 和「真的做完了」长得一模一样。没有这个字段，看板上没法分辨。
ENDED_BY = frozenset({
    "finish_tool",      # 模型主动调用 finish 声明完成
    "text_response",    # 模型直接给出了文本回复（未调用 finish，兼容旧行为）
    "iteration_cap",    # 跑满最大迭代次数仍未得出结论
    "empty_output",     # 模型既没调工具也没给内容
})

# 这些结束方式意味着「没做完」——有产出但没达目的，或者压根没产出。
# 它们该映射成 incomplete 而不是 completed，也不是 failed：
# 什么都没坏，任务就是没完成。
INCOMPLETE_ENDED_BY = frozenset({"iteration_cap", "empty_output"})


def _now_ms() -> float:
    """相对单调时钟（毫秒），用于计算耗时，不受系统时间调整影响。"""
    return time.monotonic() * 1000.0


def _wall_ts() -> str:
    """带时区信息的墙钟时间，用于日志展示。"""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _safe_json(obj: Any) -> Any:
    """把对象转成可 JSON 序列化的结构（尽力而为，失败则转字符串）。"""
    try:
        json.dumps(obj, ensure_ascii=False, default=str)
        return obj
    except (TypeError, ValueError):
        return str(obj)


def _source_from_run_start(events: list["TraceEvent"]) -> str:
    """从 run_start 事件的 input 里取来源系统，取不到返回空串。

    这是给「Run.source 字段存在之前落盘的老记录」用的回填路径：那时候
    只有 ingest 会写来源，而且写在事件里。input 可能是被 _safe_json
    转成的字符串，所以要判类型再取。
    """
    for e in events:
        if e.type == "run_start" and isinstance(e.input, dict):
            src = e.input.get("source")
            if isinstance(src, str) and src:
                return src
    return ""


@dataclass
class TraceEvent:
    """单条追踪事件。"""

    id: str
    run_id: str
    parent_id: Optional[str]
    type: str
    name: str
    ts_ms: float            # 单调时钟，用于计算耗时
    wall_ts: str            # 墙钟时间，用于展示
    input: Any = None
    output: Any = None
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        # 裁剪过大的上下文，避免落盘/传输爆炸
        d["input"] = _safe_json(self.input)
        d["output"] = _safe_json(self.output)
        return d


@dataclass
class PendingApproval:
    """一次等待人工审批的挂起请求。

    future 由发起审批的协程 await，由 HTTP 接口 / 超时 / 删除来解除。
    注意：注册表这边的操作一律同步，绝不加锁——见 TraceStore 里的说明。
    """

    approval_id: str
    run_id: str
    future: "asyncio.Future"
    created_ms: float


@dataclass
class Run:
    """一次 agent 运行的汇总信息。"""

    id: str
    task: str
    status: str = "running"          # 取值见 RUN_STATUSES
    model: str = ""
    # 这条 run 来自哪个接入的系统：看板自建是 "dashboard"，
    # 外部项目上报的是它自己的 key（见 config.SYSTEMS）。
    # 默认空串而不是 dashboard——外部客户端漏传 source 时不该被静默算成看板自建。
    source: str = ""
    started_at: str = ""
    finished_at: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    final_output: Any = None
    error: Optional[str] = None
    ended_by: Optional[str] = None   # 取值见 ENDED_BY

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "task": self.task,
            "status": self.status,
            "model": self.model,
            "source": self.source,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "final_output": _safe_json(self.final_output),
            "error": self.error,
            "ended_by": self.ended_by,
        }


class TraceStore:
    """线程安全的内存事件仓库，负责落盘与历史加载。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._runs: dict[str, Run] = {}
        self._events: dict[str, list[TraceEvent]] = {}
        self._pending: dict[str, PendingApproval] = {}
        self._load_history()

    # ---- 查询 ----

    def list_runs(self) -> list[dict]:
        runs = sorted(self._runs.values(), key=lambda r: r.started_at, reverse=True)
        return [r.to_dict() for r in runs]

    def get_run(self, run_id: str) -> Optional[Run]:
        return self._runs.get(run_id)

    def get_events(self, run_id: str) -> list[TraceEvent]:
        return list(self._events.get(run_id, []))

    # ---- 写入（由 agent 循环调用） ----

    async def start_run(self, task: str, model: str, source: str) -> Run:
        """看板自建 run。source 必传（调用方传 config.DASHBOARD_SOURCE）——
        这里不给默认值，是为了让「这条算哪个系统的」永远是个显式决定。"""
        async with self._lock:
            run = Run(
                id=uuid.uuid4().hex,
                task=task,
                model=model,
                source=source,
                started_at=_wall_ts(),
            )
            self._runs[run.id] = run
            self._events[run.id] = []
            self._append_event_locked(
                TraceEvent(
                    id=uuid.uuid4().hex,
                    run_id=run.id,
                    parent_id=None,
                    type="run_start",
                    name="run_start",
                    ts_ms=_now_ms(),
                    wall_ts=_wall_ts(),
                    input={"task": task, "model": model, "source": source},
                )
            )
            return run

    async def set_status(self, run_id: str, status: str) -> bool:
        """除终态方法外唯一的状态写入口（如 running <-> awaiting_approval）。

        锁内是纯同步代码，不 await，因此不会与审批等待互相阻塞。
        返回是否写入成功：未知 run、或已是终态的 run 都拒绝修改。
        """
        if status not in RUN_STATUSES:
            raise ValueError(f"未知状态: {status}")
        async with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.status in TERMINAL_STATUSES:
                return False
            run.status = status
            return True

    # ---- 审批注册表 ----
    # 全部是同步方法，刻意不加锁：等待审批的协程要在锁外挂起，
    # 否则会冻住整个 store 的写入——包括那个用来解除等待的 HTTP 接口。

    def register_approval(self, approval_id: str, run_id: str) -> asyncio.Future:
        """先注册 future，再写 approval_request 事件，最后才 await。

        顺序不能反：若先写事件，前端可能已经看到并发来决定，
        而此时 future 还不存在，那个决定就丢了。
        """
        fut = asyncio.get_running_loop().create_future()
        self._pending[approval_id] = PendingApproval(
            approval_id=approval_id, run_id=run_id,
            future=fut, created_ms=_now_ms(),
        )
        return fut

    def unregister_approval(self, approval_id: str) -> None:
        self._pending.pop(approval_id, None)

    def resolve_approval(self, approval_id: str, run_id: str, decision: Any) -> bool:
        """由 HTTP 接口调用。返回是否成功解除等待。

        done() 检查不可省：超时会让 wait_for 取消这个 future，
        此时再 set_result 会抛 InvalidStateError（接口变成 500）。
        返回 False 让调用方给出 409，语义上也更诚实——
        这次审批已经结束（超时 / 已被决定 / run 已删除）。
        """
        p = self._pending.get(approval_id)
        if p is None or p.run_id != run_id or p.future.done():
            return False
        p.future.set_result(decision)
        return True

    def drop_pending_for_run(self, run_id: str) -> int:
        """run 被删除时清掉它的挂起审批，避免残留条目。"""
        stale = [k for k, p in self._pending.items() if p.run_id == run_id]
        for k in stale:
            self._pending.pop(k, None)
        return len(stale)

    def pending_for_run(self, run_id: str) -> Optional[PendingApproval]:
        for p in self._pending.values():
            if p.run_id == run_id:
                return p
        return None

    async def finish_run(
        self,
        run_id: str,
        final_output: Any = None,
        error: Optional[str] = None,
        ended_by: Optional[str] = None,
        status: Optional[str] = None,
    ) -> None:
        """结束一次运行。

        status 不传时按 error 推断（有错 failed / 无错 completed）。
        调用方可显式传 incomplete，表示「没做完，但也不是出错」——
        这是本层要能表达的核心区别。
        """
        async with self._lock:
            run = self._runs.get(run_id)
            # 幂等：已是终态的 run 不再追加第二个 run_end、也不重复落盘
            if run is None or run.status in TERMINAL_STATUSES:
                return
            run.finished_at = _wall_ts()
            run.final_output = final_output
            run.ended_by = ended_by
            if status is not None:
                if status not in TERMINAL_STATUSES:
                    raise ValueError(f"finish_run 只能以终态结束: {status}")
                run.status = status
                if error is not None:
                    run.error = error
            elif error is not None:
                run.status = "failed"
                run.error = error
            else:
                run.status = "completed"
            self._append_event_locked(
                TraceEvent(
                    id=uuid.uuid4().hex,
                    run_id=run_id,
                    parent_id=None,
                    type="run_end",
                    name="run_end",
                    ts_ms=_now_ms(),
                    wall_ts=_wall_ts(),
                    output=final_output,
                    meta={
                        k: v for k, v in
                        {"error": error, "ended_by": ended_by, "status": run.status}.items()
                        if v is not None
                    },
                )
            )
            self._dump_run_locked(run_id)

    async def append_event(
        self,
        run_id: str,
        type: str,
        name: str,
        parent_id: Optional[str] = None,
        input: Any = None,
        output: Any = None,
        meta: Optional[dict] = None,
    ) -> TraceEvent:
        """追加一条事件，返回该事件（含生成的 id）。"""
        async with self._lock:
            ev = TraceEvent(
                id=uuid.uuid4().hex,
                run_id=run_id,
                parent_id=parent_id,
                type=type,
                name=name,
                ts_ms=_now_ms(),
                wall_ts=_wall_ts(),
                input=input,
                output=output,
                meta=meta or {},
            )
            self._append_event_locked(ev)
            return ev

    async def add_tokens(self, run_id: str, input_tokens: int, output_tokens: int) -> None:
        async with self._lock:
            run = self._runs.get(run_id)
            if run is not None:
                run.input_tokens += input_tokens
                run.output_tokens += output_tokens

    async def log(self, run_id: str, message: str, level: str = "info") -> None:
        await self.append_event(run_id, "log", message, meta={"level": level})

    async def delete_run(self, run_id: str) -> bool:
        """删除一条历史 run（内存 + 落盘文件）。返回是否删除成功。"""
        async with self._lock:
            run = self._runs.pop(run_id, None)
            self._events.pop(run_id, None)
            if run is None:
                return False
            try:
                (TRACES_DIR / f"{run_id}.json").unlink(missing_ok=True)
            except Exception:  # 文件删除失败不影响内存清理
                pass
            return True

    # ---- 外部 ingest（供 canvas 等外部 agent 上报）----

    async def ingest_start_run(self, run_id: str, task: str, model: str, source: str = "canvas") -> Run:
        """外部上报：创建一条 run（幂等，客户端重试直接复用）。

        source 默认 "canvas" 是给老客户端留的兼容值，**不要改**：改掉会静默
        把没传 source 的上报改归到别的系统名下。新接入的项目应显式传自己的 key。
        """
        async with self._lock:
            run = self._runs.get(run_id)
            if run is not None:
                return run
            run = Run(
                id=run_id,
                task=task,
                model=model,
                source=source,
                started_at=_wall_ts(),
            )
            self._runs[run_id] = run
            self._events[run_id] = []
            self._append_event_locked(
                TraceEvent(
                    id=uuid.uuid4().hex,
                    run_id=run_id,
                    parent_id=None,
                    type="run_start",
                    name="run_start",
                    ts_ms=_now_ms(),
                    wall_ts=_wall_ts(),
                    input={"task": task, "model": model, "source": source},
                )
            )
            return run

    async def ingest_append_events(self, run_id: str, events: list[dict]) -> list[TraceEvent]:
        """外部上报：追加事件数组（客户端可提供 id/parent_id）。"""
        async with self._lock:
            if run_id not in self._runs:
                return []
            out = []
            for e in events:
                ev = TraceEvent(
                    id=e.get("id") or uuid.uuid4().hex,
                    run_id=run_id,
                    parent_id=e.get("parent_id"),
                    type=e["type"],
                    name=e.get("name", e["type"]),
                    ts_ms=e.get("ts_ms", _now_ms()),
                    wall_ts=e.get("wall_ts") or _wall_ts(),
                    input=e.get("input"),
                    output=e.get("output"),
                    meta=e.get("meta") or {},
                )
                self._append_event_locked(ev)
                out.append(ev)
            return out

    async def ingest_finish_run(
        self,
        run_id: str,
        final_output: Any = None,
        error: Optional[str] = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        ended_by: Optional[str] = None,
    ) -> bool:
        """外部上报：结束一条 run，置状态/tokens，落盘。

        ended_by 让上报方能如实表达「怎么结束的」。没有它的话，
        上报端只能二选一：不传 error（记成 completed，哪怕其实跑到
        迭代上限没做完），或传一个假 error（记成 failed，其实没出错）。
        两种都是在撒谎。见 tracer.ENDED_BY。
        """
        async with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return False
            run.finished_at = _wall_ts()
            run.final_output = final_output
            run.input_tokens = input_tokens
            run.output_tokens = output_tokens
            run.ended_by = ended_by
            if error is not None:
                run.status = "failed"
                run.error = error
            elif ended_by in INCOMPLETE_ENDED_BY:
                # 没做完，但也不是出错——这是「完成」与「失败」之间的第三态
                run.status = "incomplete"
            else:
                run.status = "completed"
            self._append_event_locked(
                TraceEvent(
                    id=uuid.uuid4().hex,
                    run_id=run_id,
                    parent_id=None,
                    type="run_end",
                    name="run_end",
                    ts_ms=_now_ms(),
                    wall_ts=_wall_ts(),
                    output=final_output,
                    meta={
                        k: v for k, v in
                        {"error": error, "ended_by": ended_by, "status": run.status}.items()
                        if v is not None
                    },
                )
            )
            self._dump_run_locked(run_id)
            return True

    def stats(self) -> dict:
        """聚合统计：供统计页展示。读操作，无需加锁。"""
        runs = list(self._runs.values())
        status_counts = {
            "completed": 0, "incomplete": 0, "failed": 0,
            "running": 0, "awaiting_approval": 0,
        }
        total_in = 0
        total_out = 0
        model_counts: dict[str, int] = {}
        source_counts: dict[str, int] = {}
        for r in runs:
            status_counts[r.status] = status_counts.get(r.status, 0) + 1
            total_in += r.input_tokens
            total_out += r.output_tokens
            m = r.model or "unknown"
            model_counts[m] = model_counts.get(m, 0) + 1
            # 理论上 _load_history 已把空 source 归一化过；这里再兜一次底，
            # 免得内存里真有漏网的 run 在图上变成一个没名字的空条目。
            # 字面量与 config.DASHBOARD_SOURCE 同值，由 devtools/test_systems.py 钉住
            s = r.source or "dashboard"
            source_counts[s] = source_counts.get(s, 0) + 1

        llm_calls = 0
        tool_calls = 0
        latencies: list[float] = []
        for run_id, evs in self._events.items():
            # 已删除的 run 若还有后台任务在写，事件会被 setdefault 复活；
            # 那些幽灵事件不属于任何可见的 run，不能计入统计
            if run_id not in self._runs:
                continue
            for e in evs:
                if e.type == "llm_call":
                    llm_calls += 1
                elif e.type == "tool_call":
                    tool_calls += 1
                elif e.type == "llm_response":
                    lat = (e.meta or {}).get("latency_ms")
                    if isinstance(lat, (int, float)):
                        latencies.append(lat)

        avg_latency = round(sum(latencies) / len(latencies), 1) if latencies else 0
        recent = sorted(runs, key=lambda r: r.started_at, reverse=True)[:10]

        return {
            "total_runs": len(runs),
            "status_counts": status_counts,
            "total_input_tokens": total_in,
            "total_output_tokens": total_out,
            "model_counts": model_counts,
            # 按数量降序：条形图直接照顺序画，前端不用再排
            "source_counts": dict(sorted(source_counts.items(), key=lambda kv: -kv[1])),
            "total_llm_calls": llm_calls,
            "total_tool_calls": tool_calls,
            "avg_latency_ms": avg_latency,
            "recent_runs": [r.to_dict() for r in recent],
        }

    # ---- 内部 ----

    def _append_event_locked(self, ev: TraceEvent) -> None:
        self._events.setdefault(ev.run_id, []).append(ev)

    def _dump_run_locked(self, run_id: str) -> None:
        """把一次运行完整落盘到 traces/<run_id>.json。"""
        try:
            TRACES_DIR.mkdir(parents=True, exist_ok=True)
            run = self._runs.get(run_id)
            events = self._events.get(run_id, [])
            payload = {
                "run": run.to_dict() if run else None,
                "events": [e.to_dict() for e in events],
            }
            path = TRACES_DIR / f"{run_id}.json"
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:  # 落盘失败不影响主流程
            pass

    def _load_history(self) -> None:
        """启动时扫描 traces/ 目录，加载历史 run（跨重启可查看）。"""
        if not TRACES_DIR.exists():
            return
        for p in sorted(TRACES_DIR.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                run_data = data.get("run")
                if not run_data:
                    continue
                # 事件必须先于 Run 构造：下面要靠 run_start 事件回填 source
                events = [
                    TraceEvent(
                        id=e["id"],
                        run_id=e["run_id"],
                        parent_id=e.get("parent_id"),
                        type=e["type"],
                        name=e.get("name", e["type"]),
                        ts_ms=e.get("ts_ms", 0.0),
                        wall_ts=e.get("wall_ts", ""),
                        input=e.get("input"),
                        output=e.get("output"),
                        meta=e.get("meta", {}),
                    )
                    for e in data.get("events", [])
                ]
                run = Run(
                    id=run_data["id"],
                    task=run_data.get("task", ""),
                    status=run_data.get("status", "completed"),
                    model=run_data.get("model", ""),
                    # source 是后加的字段，老记录都没存；那时只有 ingest 路径会
                    # 把来源写进 run_start 事件，所以先查事件，再退回看板自建。
                    # 只补内存不重写文件——启动时批量改历史文件是隐性副作用，
                    # 下次 finish_run 落盘时自然就带上了。
                    source=run_data.get("source") or _source_from_run_start(events) or "dashboard",
                    started_at=run_data.get("started_at", ""),
                    finished_at=run_data.get("finished_at"),
                    input_tokens=run_data.get("input_tokens", 0),
                    output_tokens=run_data.get("output_tokens", 0),
                    final_output=run_data.get("final_output"),
                    error=run_data.get("error"),
                    # 旧记录没有这个字段，为 None 即可
                    ended_by=run_data.get("ended_by"),
                )
                self._runs[run.id] = run
                self._events[run.id] = events
            except Exception:
                continue


# 全局单例
store = TraceStore()
