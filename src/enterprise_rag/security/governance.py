"""租户配额、并发准入和安全审计事件。"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import asdict, dataclass
from threading import Lock
from typing import Any

from .context import RequestContext


class TenantQuotaExceeded(PermissionError):
    """租户超过服务端配额，不能继续调用昂贵的检索/入库流程。"""


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """不保存正文和 token 的安全审计事件。"""

    event: str
    actor_id: str
    tenant_id: str
    operation: str
    allowed: bool
    reason: str
    created_at: float

    def as_dict(self) -> dict[str, Any]:
        """返回可写入审计 sink 的稳定结构。"""

        return asdict(self)


class InMemoryAuditSink:
    """开发/测试用有界审计 sink；生产可替换为 PostgreSQL 或日志管道。"""

    def __init__(self, *, max_events: int = 2048) -> None:
        """创建有界事件缓冲，避免审计失败拖垮业务进程。"""

        if max_events <= 0:
            raise ValueError("max_events 必须大于 0")
        self.max_events = max_events
        self._events: deque[AuditEvent] = deque(maxlen=max_events)
        self._lock = Lock()

    def append(self, event: AuditEvent) -> None:
        """追加安全事件，不写入请求正文或凭据。"""

        with self._lock:
            self._events.append(event)

    def snapshot(self) -> list[dict[str, Any]]:
        """复制事件摘要，供测试和运维采集。"""

        with self._lock:
            return [event.as_dict() for event in self._events]


class TenantGovernance:
    """执行租户级 fixed-window 配额并记录 allow/deny 审计。"""

    def __init__(
        self,
        *,
        requests_per_minute: int = 600,
        uploads_per_minute: int = 120,
        audit_sink: InMemoryAuditSink | None = None,
    ) -> None:
        """初始化保守的开发默认值；生产应由部署配置覆盖。"""

        if requests_per_minute <= 0 or uploads_per_minute <= 0:
            raise ValueError("租户配额必须大于 0")
        self.requests_per_minute = requests_per_minute
        self.uploads_per_minute = uploads_per_minute
        self.audit_sink = audit_sink or InMemoryAuditSink()
        self._windows: dict[tuple[str, str], deque[float]] = {}
        self._lock = Lock()

    async def check(self, context: RequestContext, operation: str) -> None:
        """检查租户操作配额，拒绝时只返回固定类别。"""

        limit = self.uploads_per_minute if operation == "document.upload" else self.requests_per_minute
        now = time.monotonic()
        key = (context.tenant_id, operation)
        with self._lock:
            window = self._windows.setdefault(key, deque())
            while window and now - window[0] >= 60:
                window.popleft()
            allowed = len(window) < limit
            if allowed:
                window.append(now)
        self.audit_sink.append(
            AuditEvent(
                event="tenant_quota_check",
                actor_id=context.actor_id,
                tenant_id=context.tenant_id,
                operation=operation,
                allowed=allowed,
                reason="within_quota" if allowed else "quota_exceeded",
                created_at=time.time(),
            )
        )
        if not allowed:
            raise TenantQuotaExceeded("租户操作频率受限")

    def snapshot(self) -> dict[str, Any]:
        """返回不含正文的治理配置和审计计数。"""

        return {
            "requests_per_minute": self.requests_per_minute,
            "uploads_per_minute": self.uploads_per_minute,
            "audit_events": len(self.audit_sink.snapshot()),
        }


__all__ = [
    "AuditEvent",
    "InMemoryAuditSink",
    "TenantGovernance",
    "TenantQuotaExceeded",
]
