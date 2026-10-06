"""异步摄取新增路由，保留原同步入口的路径与响应。"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from enterprise_rag.ingestion.task_ledger import TaskConflict
from enterprise_rag.security.authorization import AuthorizationDenied, ResourceNotFound


def ingestion_router(context_dependency):
    """复用产品认证依赖；未装配 service 时明确 503。"""
    router = APIRouter(prefix="/api/v1/ingestion/tasks", tags=["ingestion"])

    def service(request):
        """只读取服务器注入服务，不接受客户端提供连接或身份。"""
        resolved = getattr(request.app.state, "async_ingestion_service", None)
        if resolved is None:
            raise HTTPException(503, detail={"kind": "ingestion_unavailable"})
        return resolved

    async def invoke(operation):
        """归一化领域异常，不回显原文、object locator 或上游异常。"""
        try:
            return asdict(await operation)
        except AuthorizationDenied:
            raise HTTPException(403, detail={"kind": "forbidden"}) from None
        except ResourceNotFound:
            raise HTTPException(404, detail={"kind": "resource_not_found"}) from None
        except TaskConflict:
            raise HTTPException(409, detail={"kind": "task_conflict"}) from None
        except ValueError:
            raise HTTPException(422, detail={"kind": "invalid_document"}) from None
        except Exception:  # noqa: BLE001 - HTTP 边界统一脱敏，取消不在 Exception 内。
            raise HTTPException(503, detail={"kind": "ingestion_unavailable"}) from None

    @router.post("", status_code=202)
    async def accept(
        request: Request, context=Depends(context_dependency),  # noqa: B008
        file: UploadFile = File(...), source_id: str = Form(...),  # noqa: B008
        title: str = Form(...), idempotency_key: str = Form(...),
    ):
        """受理返回 202 与任务 ID，不等待解析或模型，不自报 tenant。"""
        form = await request.form()
        if set(form) - {"file", "source_id", "title", "idempotency_key"}:
            raise HTTPException(422, detail={"kind": "invalid_request"})
        coordinator = getattr(request.app.state, "redis_coordinator", None)
        if coordinator is not None:
            try:
                allowed = await coordinator.allow(
                    context.tenant_id, "ingestion", capacity=10, refill_per_second=2,
                )
            except Exception:  # noqa: BLE001 - 准入协调失效不能静默放行。
                raise HTTPException(503, detail={"kind": "admission_unavailable"}) from None
            if not allowed:
                raise HTTPException(429, detail={"kind": "rate_limited"}, headers={"Retry-After": "1"})
        return await invoke(service(request).accept(
            context, file, source_id=source_id, title=title, idempotency_key=idempotency_key,
        ))

    @router.get("/{task_id}")
    async def status(request: Request, task_id: str, context=Depends(context_dependency)):  # noqa: B008
        """任务长期状态经 owner/tenant 复核，只返回安全摘要。"""
        return await invoke(service(request).get(context, task_id))

    @router.post("/{task_id}/replay", status_code=202)
    async def replay(request: Request, task_id: str, context=Depends(context_dependency)):  # noqa: B008
        """显式人工 replay 终态死信，保留历史执行。"""
        return await invoke(service(request).replay(context, task_id))

    return router
