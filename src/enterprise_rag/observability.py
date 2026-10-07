"""低依赖、脱敏的请求级 Observability。

该模块不依赖 Prometheus SDK，先提供稳定的内部指标和 trace span 契约。
部署层可以把 ``snapshot`` 转换为 Prometheus/OpenTelemetry，但应用本身不记录
正文、token 内容、密钥或无界 tenant/task label。
"""

from __future__ import annotations

import math
import re
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock
from typing import Any
from uuid import uuid4

ASGIMessage = dict[str, Any]
Receive = Callable[[], Awaitable[ASGIMessage]]
Send = Callable[[ASGIMessage], Awaitable[None]]
_TRACE: ContextVar[TraceRecorder | None] = ContextVar("rag_trace", default=None)
_ID_PATTERN = re.compile(r"^(?:task_|doc_|ver_|job_|gen_)[A-Za-z0-9_-]+$")


def _nearest_rank(samples: list[float], percentile: float) -> float | None:
    """使用 nearest-rank 计算有界样本的百分位，空样本返回 None。"""

    if not samples:
        return None
    ordered = sorted(samples)
    position = max(1, math.ceil(len(ordered) * percentile)) - 1
    return ordered[min(position, len(ordered) - 1)]


def current_trace() -> TraceRecorder | None:
    """返回当前请求 trace；后台任务没有请求上下文时返回 None。"""

    return _TRACE.get()


def _safe_path(path: str) -> str:
    """把资源 ID 归一化为低基数指标路径。"""

    parts = []
    for part in path.split("/"):
        if _ID_PATTERN.fullmatch(part) or len(part) > 48:
            parts.append(":id")
        else:
            parts.append(part)
    return "/".join(parts) or "/"


class MetricsRegistry:
    """进程内有界计数器和直方图；不把高基数输入写入 label。"""

    def __init__(self, *, histogram_limit: int = 256) -> None:
        """初始化线程安全的指标容器。"""

        if histogram_limit <= 0:
            raise ValueError("histogram_limit 必须大于 0")
        self._lock = Lock()
        self._histogram_limit = histogram_limit
        self._counters: dict[str, int] = defaultdict(int)
        self._histograms: dict[str, list[float]] = defaultdict(list)

    def increment(self, name: str, amount: int = 1) -> None:
        """增加一个低基数计数器。"""

        if not name.strip() or amount < 0:
            raise ValueError("指标名称或增量非法")
        with self._lock:
            self._counters[name] += amount

    def observe(self, name: str, value: float) -> None:
        """记录有界耗时样本，超出容量时丢弃最旧样本。"""

        if (
            not name.strip()
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError("指标名称或观测值非法")
        with self._lock:
            samples = self._histograms[name]
            samples.append(round(value, 3))
            if len(samples) > self._histogram_limit:
                del samples[: len(samples) - self._histogram_limit]

    def snapshot(self) -> dict[str, Any]:
        """复制当前指标，供健康检查或监控采集使用。"""

        with self._lock:
            return {
                "counters": dict(sorted(self._counters.items())),
                "histograms": {
                    name: {
                        "count": len(samples),
                        "sample_count": len(samples),
                        "last_ms": samples[-1] if samples else None,
                        "max_ms": max(samples) if samples else None,
                        "p50_nearest_rank": _nearest_rank(samples, 0.50),
                        "p95_nearest_rank": _nearest_rank(samples, 0.95),
                    }
                    for name, samples in sorted(self._histograms.items())
                },
            }


@dataclass(slots=True)
class TraceSpan:
    """一个脱敏阶段 span。"""

    name: str
    started_at: float
    ended_at: float | None = None
    attributes: dict[str, str | int | float | bool] = field(default_factory=dict)

    def finish(self) -> None:
        """记录结束时间，不重复覆盖第一次完成时间。"""

        if self.ended_at is None:
            self.ended_at = time.monotonic()

    def as_dict(self) -> dict[str, Any]:
        """转换为不包含正文的 span 摘要。"""

        ended = self.ended_at or time.monotonic()
        return {
            "name": self.name,
            "duration_ms": round(max(0.0, ended - self.started_at) * 1000, 3),
            "attributes": dict(self.attributes),
        }


class TraceRecorder:
    """记录单个请求的 trace_id、阶段耗时和安全结果标签。"""

    def __init__(self, trace_id: str, metrics: MetricsRegistry) -> None:
        """创建绑定 metrics registry 的请求 trace。"""

        self.trace_id = trace_id
        self._metrics = metrics
        self._spans: list[TraceSpan] = []
        self._attributes: dict[str, str | int | float | bool] = {}

    def set_attribute(self, name: str, value: str | float | bool) -> None:
        """写入低基数属性；禁止把正文或身份 token 放入 trace。"""

        if not name.strip():
            raise ValueError("trace 属性名不能为空")
        if len(str(value)) > 128:
            raise ValueError("trace 属性值过长")
        self._attributes[name] = value

    @contextmanager
    def span(self, name: str, **attributes: str | float | bool):
        """记录一个同步代码块的阶段耗时，并同步到 histogram。"""

        span = TraceSpan(name=name, started_at=time.monotonic(), attributes=attributes)
        self._spans.append(span)
        try:
            yield span
        finally:
            span.finish()
            self._metrics.observe(f"rag_stage_duration_ms:{name}", span.as_dict()["duration_ms"])

    def snapshot(self, *, status_code: int | None = None) -> dict[str, Any]:
        """返回安全的 trace 摘要，默认不包含问题、答案和证据正文。"""

        payload: dict[str, Any] = {
            "trace_id": self.trace_id,
            "attributes": dict(self._attributes),
            "spans": [span.as_dict() for span in self._spans],
        }
        if status_code is not None:
            payload["status_code"] = status_code
        return payload


class RequestObservabilityMiddleware:
    """为每个 HTTP 请求生成 trace/request ID、响应头和聚合指标。"""

    def __init__(self, app: Callable[..., Awaitable[None]], metrics: MetricsRegistry) -> None:
        """绑定 ASGI app 和共享指标注册表。"""

        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        """包裹请求生命周期并在不泄露正文的前提下采集状态和耗时。"""

        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = {
            key.lower(): value.decode("latin1")
            for key, value in scope.get("headers", [])
        }
        request_id = headers.get("x-request-id") or str(uuid4())
        trace_id = headers.get("x-trace-id") or str(uuid4())
        recorder = TraceRecorder(trace_id, self.metrics)
        recorder.set_attribute("method", scope.get("method", "UNKNOWN"))
        recorder.set_attribute("path", _safe_path(scope.get("path", "/")))
        scope.setdefault("state", {})["request_id"] = request_id
        scope["state"]["trace_id"] = trace_id
        scope["state"]["trace"] = recorder
        token = _TRACE.set(recorder)
        started = time.monotonic()
        status_code = 500

        async def observed_send(message: ASGIMessage) -> None:
            """注入安全响应头，并捕获最终 HTTP 状态码。"""

            nonlocal status_code
            if message.get("type") == "http.response.start":
                status_code = int(message.get("status", 500))
                response_headers = list(message.get("headers", []))
                response_headers.extend(
                    [
                        (b"x-request-id", request_id.encode("ascii")),
                        (b"x-trace-id", trace_id.encode("ascii")),
                    ]
                )
                message = {**message, "headers": response_headers}
            await send(message)

        try:
            await self.app(scope, receive, observed_send)
        finally:
            duration_ms = max(0.0, (time.monotonic() - started) * 1000)
            path = _safe_path(scope.get("path", "/"))
            self.metrics.increment(f"http_requests_total:{scope.get('method', 'UNKNOWN')}:{path}:{status_code}")
            self.metrics.observe("http_request_duration_ms", duration_ms)
            recorder.set_attribute("status_code", status_code)
            self.metrics.increment(f"trace_status_total:{status_code}")
            _TRACE.reset(token)


__all__ = [
    "MetricsRegistry",
    "RequestObservabilityMiddleware",
    "TraceRecorder",
    "current_trace",
]
