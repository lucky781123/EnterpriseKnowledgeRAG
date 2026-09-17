"""FastAPI 应用入口。

除健康检查外，本模块提供 F2-G 的 JSON/SSE 原文摘录入口。路由只负责
HTTP/Pydantic、生命周期、调度和事件编码；Prompt、原文引用 identity 校验仍由
``ExcerptAnswerService`` 负责，Provider 只负责模型传输。

本模块特意把“模型调用成功”和“答案通过引用校验”分成两个状态：前者由
``provider_result`` 表达，后者由 ``answer_status``/``citation_validation`` 表达。
这样上层不会因为模型返回了合法 JSON，就误把未经 identity 校验的内容当成可信答案。
"""

from __future__ import annotations

import asyncio
import inspect
import time
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select

from enterprise_rag.config.settings import Settings
from enterprise_rag.db.models import (
    ACLPrincipalType,
    ArtifactKind,
    DenseGenerationModel,
    DocumentModel,
    DocumentVersionModel,
    IngestionJobModel,
    VersionArtifactModel,
)
from enterprise_rag.db.session import Database
from enterprise_rag.embedding.ollama import OllamaEmbeddingProvider
from enterprise_rag.excerpts.service import ExcerptAnswerService, VisibleStreamSanitizer
from enterprise_rag.excerpts.validation import ExcerptValidationError
from enterprise_rag.ingestion.product_service import (
    ProductIndexingError,
)
from enterprise_rag.ingestion.service import IdempotencyConflictError, IngestionServiceError
from enterprise_rag.ingestion.validation import IngestionValidationError
from enterprise_rag.providers.errors import ProviderError, ProviderErrorKind
from enterprise_rag.providers.lmstudio import LMStudioChatProvider
from enterprise_rag.providers.protocols import ChatResponse, ChatStreamEvent, ChatUsage
from enterprise_rag.providers.readiness import ProviderReadinessManager
from enterprise_rag.providers.registry import ProviderRegistry, RegisteredProvider
from enterprise_rag.providers.scheduler import LocalLLMScheduler, SchedulerLease
from enterprise_rag.retrieval.repository import PostgresVectorRepository
from enterprise_rag.security.authorization import (
    AuthorizationDenied,
    AuthorizationService,
    ResourceNotFound,
)
from enterprise_rag.security.dev_token import DevTokenCodec

from .dependencies import build_request_context_dependency
from .middleware import RequestBodyLimitMiddleware
from .product_readiness import ProductReadinessManager
from .product_schemas import (
    ProductACLGrantResponse,
    ProductACLRevokeResponse,
    ProductArtifactStatusResponse,
    ProductDocumentStatusResponse,
    ProductGenerationStatusResponse,
    ProductJobStatusResponse,
    ProductRagQueryRequest,
    ProductRagQueryResponse,
    ProductReadinessResponse,
    ProductRetrievalTraceResponse,
    ProductUploadResponse,
    ProductVersionStatusResponse,
)
from .schemas import (
    CitationResponse,
    EvidenceRequest,
    ExcerptQueryRequest,
    ExcerptResponse,
    ProviderErrorResponse,
    ProviderResultResponse,
    RetrievalQueryRequest,
    TimingsResponse,
    UsageResponse,
)
from .sse import format_sse_event


def _elapsed_ms(started_at: float | None) -> float | None:
    """使用 monotonic clock 计算阶段耗时，避免系统时间回拨造成负数。"""

    if started_at is None:
        return None
    return max(0.0, (time.monotonic() - started_at) * 1000)


def _remaining_seconds(deadline: float) -> float:
    """返回绝对 deadline 的剩余秒数；调用方不应重新创建一份预算。"""

    return max(0.0, deadline - time.monotonic())


def _usage_payload(usage: ChatUsage | None) -> UsageResponse | None:
    """把 Provider token 计数映射为 API 类型，缺失时保持 ``null``。"""

    if usage is None:
        return None
    return UsageResponse(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
    )


def _safe_error_message(kind: ProviderErrorKind) -> str:
    """将内部错误类别映射为固定文案，绝不回显上游异常正文。"""

    return {
        ProviderErrorKind.INVALID_REQUEST: "请求参数不符合上游模型契约",
        ProviderErrorKind.RATE_LIMITED: "上游模型请求频率受限",
        ProviderErrorKind.TIMEOUT: "上游模型请求超时",
        ProviderErrorKind.AUTHENTICATION: "上游认证失败",
        ProviderErrorKind.UNAVAILABLE: "上游模型服务不可用",
        ProviderErrorKind.PROTOCOL: "上游模型响应协议错误",
        ProviderErrorKind.CANCELLED: "请求已取消",
        ProviderErrorKind.UNKNOWN: "上游模型调用失败",
    }.get(kind, "上游模型调用失败")


def _provider_error_payload(error: ProviderError) -> ProviderErrorResponse:
    """构造 ProviderResult 内嵌的安全错误摘要。"""

    return ProviderErrorResponse(
        kind=error.kind.value,
        retryable=error.retryable,
        safe_message=_safe_error_message(error.kind),
    )


def _provider_result_payload(
    *,
    request_id: str,
    provider_alias: str,
    model: str,
    status: str,
    latency_ms: float | None,
    response: ChatResponse | None = None,
    error: ProviderError | None = None,
) -> ProviderResultResponse:
    """构造共享 ProviderResult；原始模型正文永远固定为 ``None``。"""

    # latency_ms 在异常发生于调用开始前时也必须满足非负 Schema；用零表示
    # “尚未进入 Provider”比伪造一个正常生成时延更诚实。正常调用会传入实际耗时。
    safe_latency = max(0.0, latency_ms or 0.0)
    return ProviderResultResponse(
        request_id=request_id,
        provider=provider_alias,
        model=(response.model if response is not None else model),
        status=status,  # type: ignore[arg-type]
        latency_ms=safe_latency,
        # ProviderResult 只暴露元数据，不把可能含敏感证据或内部推理的正文
        # 再复制一份到 API；答案正文也不由本入口作为独立字段返回。
        content=None,
        finish_reason=response.finish_reason if response is not None else None,
        usage=_usage_payload(response.usage if response is not None else None),
        error=_provider_error_payload(error) if error is not None else None,
    )


def _timings_payload(
    *,
    queue_ms: float | None,
    ttft_ms: float | None,
    generation_ms: float | None,
    validation_ms: float | None,
    completion_ms: float | None,
) -> TimingsResponse:
    """统一构造阶段计时；未到达的阶段保持 ``null``。"""

    return TimingsResponse(
        queue_ms=queue_ms,
        ttft_ms=ttft_ms,
        generation_ms=generation_ms,
        validation_ms=validation_ms,
        completion_ms=completion_ms,
    )


async def _await_maybe(value: Any) -> Any:
    """兼容旧 Provider 返回 coroutine 的错误实现，同时不误等待 async generator。"""

    if inspect.isawaitable(value):
        return await value
    return value


async def _close_async_iterator(iterator: Any) -> None:
    """尽力关闭上游异步迭代器，避免 HTTP response 或生成槽位泄漏。"""

    close = getattr(iterator, "aclose", None)
    if close is None:
        return
    try:
        await _await_maybe(close())
    except asyncio.CancelledError:
        # 任务取消优先级高于清理异常；让取消继续向上游传播。
        raise
    except Exception:
        # 清理异常不能覆盖原始 Provider 错误，也不能把内部异常文本写入 SSE。
        return


def create_app(
    settings: Settings | None = None,
    provider_registry: ProviderRegistry | None = None,
    database: Database | None = None,
    embedding_provider: OllamaEmbeddingProvider | None = None,
    vector_repository: PostgresVectorRepository | None = None,
    weaviate_vector_repository: Any | None = None,
    product_ingestion_service: Any | None = None,
    product_retrieval_service: Any | None = None,
    authorization_service: AuthorizationService | None = None,
    dev_token_codec: DevTokenCodec | None = None,
    product_readiness_manager: ProductReadinessManager | Any | None = None,
) -> FastAPI:
    """创建应用并装配共享 Chat、Embedding、检索和产品上传依赖。"""

    resolved_settings = settings or Settings.from_env()
    resolved_dev_token_codec = dev_token_codec
    if resolved_dev_token_codec is None and resolved_settings.dev_auth_enabled:
        if resolved_settings.dev_auth_secret is None:
            raise ValueError("开发身份已启用但缺少 DEV_AUTH_SECRET")
        resolved_dev_token_codec = DevTokenCodec(
            resolved_settings.dev_auth_secret,
            ttl_seconds=resolved_settings.dev_auth_token_ttl_seconds,
        )
    shared_http_client = None
    resolved_database = database
    owns_database = False
    if resolved_database is None and resolved_settings.postgres_dsn:
        resolved_database = Database(
            resolved_settings.postgres_dsn,
            pool_size=resolved_settings.postgres_pool_size,
            max_overflow=resolved_settings.postgres_max_overflow,
            pool_timeout=resolved_settings.postgres_pool_timeout_seconds,
            pool_recycle=resolved_settings.postgres_pool_recycle_seconds,
            statement_timeout_ms=resolved_settings.postgres_statement_timeout_ms,
            lock_timeout_ms=resolved_settings.postgres_lock_timeout_ms,
        )
        owns_database = True

    registry = provider_registry or ProviderRegistry()
    if provider_registry is None:
        import httpx

        # 默认客户端的读取预算必须覆盖 API 允许的最长 300 秒请求预算；否则
        # LM Studio 仍在生成时，HTTPX 默认的短 read timeout 会先结束请求，
        # 上层只能把一次正常的慢生成误判为 504 timeout。
        shared_http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=5.0,
                read=330.0,
                write=30.0,
                pool=10.0,
            )
        )
        # 默认 Provider 只在应用创建时创建一个共享 HTTP client；路由不会按
        # 请求创建 client，避免连接池抖动和无法统一关闭的后台连接。
        registry.register(
            "local_answer",
            LMStudioChatProvider(
                shared_http_client,
                base_url=f"{resolved_settings.local_llm_base_url.rstrip('/')}/v1",
                model=resolved_settings.local_llm_model,
                api_key=resolved_settings.local_llm_api_key,
            ),
            capabilities={"chat.complete", "chat.stream"},
        )

    resolved_embedding_provider = embedding_provider
    # Weaviate repository 只允许通过显式依赖注入启用；未注入时保留 PostgreSQL
    # 默认读取路径，避免配置字段出现就悄然改变线上查询后端。
    resolved_vector_repository = weaviate_vector_repository or vector_repository
    if resolved_database is not None:
        if resolved_embedding_provider is None:
            # 默认路径复用 Chat Provider 的 HTTP client；注入自定义 Chat Registry
            # 时才创建独立 client，并仍由统一 lifespan 负责关闭。
            if shared_http_client is None:
                import httpx

                shared_http_client = httpx.AsyncClient()
            resolved_embedding_provider = OllamaEmbeddingProvider(
                shared_http_client,
                base_url=resolved_settings.ollama_base_url,
                model=resolved_settings.ollama_embedding_model,
            )
        if resolved_vector_repository is None:
            resolved_vector_repository = PostgresVectorRepository()

    provider_readiness = ProviderReadinessManager(registry)
    # 每个 alias 都有独立的调度器快照。路由之后只通过 alias 查找已注册
    # 对象，调用方不能提交自己的 endpoint、并发参数或队列配置。
    schedulers = {
        alias: LocalLLMScheduler(
            registered.provider,
            max_concurrency=resolved_settings.local_llm_max_concurrency,
            max_queue_size=resolved_settings.local_llm_max_queue_size,
        )
        for alias, registered in registry.items()
    }

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        """按依赖逆序释放 readiness、Provider client 和数据库连接池。"""

        try:
            yield
        finally:
            # readiness 可能仍有共享 probe task；先收拢它，再关闭 Provider
            # client，避免 probe 在 client 已关闭后产生未处理异常。
            await provider_readiness.close()

            # Chat 与 Embedding 可能共享同一个 HTTP client；按对象身份去重关闭，
            # 避免共享连接池被重复释放。
            closed_clients: set[int] = set()
            managed_clients = [
                *(getattr(registered.provider, "_client", None) for _, registered in registry.items()),
                getattr(resolved_embedding_provider, "client", None),
            ]
            for client in managed_clients:
                if client is None or id(client) in closed_clients:
                    continue
                close = getattr(client, "aclose", None)
                if close is None:
                    continue
                closed_clients.add(id(client))
                await close()

            if owns_database and resolved_database is not None:
                await resolved_database.dispose()

    app = FastAPI(title="EnterpriseKnowledgeRAG", version="0.1.0", lifespan=lifespan)
    # 请求体预算必须位于 FastAPI/Pydantic 解析之前。这样即使攻击者不提供
    # Content-Length，分片正文累计超过 128 KiB 时也会在业务路由前得到稳定
    # 的 413；未超限的正文由 middleware 只重放一次，避免下游看到空 body。
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=resolved_settings.api_request_body_max_bytes,
        path_limits={"/api/v1/documents": resolved_settings.product_upload_max_bytes},
    )
    app.state.settings = resolved_settings
    app.state.database = resolved_database
    app.state.embedding_provider = resolved_embedding_provider
    app.state.vector_repository = resolved_vector_repository
    app.state.weaviate_vector_repository = weaviate_vector_repository
    app.state.provider_registry = registry
    app.state.provider_readiness = provider_readiness
    app.state.schedulers = schedulers
    app.state.product_ingestion_service = product_ingestion_service
    app.state.product_retrieval_service = product_retrieval_service
    app.state.authorization_service = authorization_service or AuthorizationService()
    app.state.dev_token_codec = resolved_dev_token_codec
    app.state.product_readiness_manager = product_readiness_manager or ProductReadinessManager(
        settings=resolved_settings,
        database=resolved_database,
        provider_registry=registry,
        dev_token_codec=resolved_dev_token_codec,
        embedding_provider=resolved_embedding_provider,
        product_ingestion_service=product_ingestion_service,
        product_retrieval_service=product_retrieval_service,
    )

    def provider_error_response(error: ProviderError) -> JSONResponse:
        """将领域错误映射为稳定 HTTP 响应，并丢弃未经信任的原始异常文本。"""

        status_code = {
            ProviderErrorKind.INVALID_REQUEST: 422,
            ProviderErrorKind.RATE_LIMITED: 429,
            ProviderErrorKind.TIMEOUT: 504,
            ProviderErrorKind.AUTHENTICATION: 502,
            ProviderErrorKind.UNAVAILABLE: 503,
            ProviderErrorKind.PROTOCOL: 502,
            ProviderErrorKind.CANCELLED: 499,
            ProviderErrorKind.UNKNOWN: 500,
        }.get(error.kind, 500)
        return JSONResponse(
            status_code=status_code,
            content={
                "error": {
                    "kind": error.kind.value,
                    "message": _safe_error_message(error.kind),
                }
            },
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        """用固定错误体替换 FastAPI 默认 detail，避免回显敏感输入值。"""

        return JSONResponse(
            status_code=422,
            content={"error": {"kind": "invalid_request", "message": "请求参数不符合接口契约"}},
        )

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        """liveness 只证明进程事件循环可响应，不调用数据库或模型。"""

        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready(response: Response) -> dict[str, object]:
        """同时检查数据库 schema 和 Provider 可达性，但绝不执行 migration。"""

        # manager 自带 5 秒缓存、并发 probe 合并和 2 秒单 Provider timeout；
        # readiness 路由不能绕过它直接调用 health_check，否则并发探测会压垮
        # 本地服务，也会让取消一个 HTTP 请求连带取消其他请求的 probe。
        provider_status = await app.state.provider_readiness.check()
        provider_payload = {"status": provider_status.status}

        if app.state.database is None:
            response.status_code = 503
            return {
                "status": "not_ready",
                "environment": app.state.settings.app_env,
                "database": {"status": "not_configured"},
                "provider": provider_payload,
            }

        readiness = await app.state.database.schema_readiness()
        if not readiness.ready or provider_status.status != "ready":
            response.status_code = 503
            database_payload: dict[str, object] = {"status": readiness.status}
            return {
                "status": "not_ready",
                "environment": app.state.settings.app_env,
                "database": database_payload,
                "provider": provider_payload,
            }

        return {
            "status": "ok",
            "environment": app.state.settings.app_env,
            "database": {
                "status": readiness.status,
                "migration": readiness.target_version,
            },
            "provider": provider_payload,
        }

    if resolved_dev_token_codec is not None:
        product_context_dependency = build_request_context_dependency(resolved_dev_token_codec)
    else:

        async def product_context_dependency() -> Any:
            """未显式配置开发身份时，产品入口保持 fail closed。"""

            raise HTTPException(
                status_code=503,
                detail={
                    "kind": "product_unavailable",
                    "message": "开发身份未配置",
                },
            )

    @app.get("/health/product/ready", response_model=ProductReadinessResponse)
    async def product_ready(response: Response) -> ProductReadinessResponse:
        """返回本地 P0 产品逐项 readiness；未就绪时稳定返回 503。"""

        readiness = await app.state.product_readiness_manager.check()
        response.status_code = 200 if readiness.status == "ready" else 503
        return ProductReadinessResponse(
            status=readiness.status,
            dependencies={
                name: {"status": status}
                for name, status in readiness.dependencies.items()
            },
        )

    @app.get(
        "/api/v1/documents/{document_id}/status",
        response_model=ProductDocumentStatusResponse,
    )
    async def document_status(
        document_id: str,
        context=Depends(product_context_dependency),  # noqa: B008
    ) -> ProductDocumentStatusResponse:
        """返回授权文档的安全状态摘要，不暴露正文和外部索引内部字段。"""

        database_dependency = app.state.database
        if database_dependency is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "kind": "product_unavailable",
                    "message": "产品数据库未配置",
                },
            )

        try:
            async with database_dependency.session() as session:
                authorized = await app.state.authorization_service.require_document_read(
                    session,
                    context,
                    document_id,
                )
                document = (
                    await session.execute(
                        select(DocumentModel).where(
                            DocumentModel.tenant_id == context.tenant_id,
                            DocumentModel.id == document_id,
                        )
                    )
                ).scalar_one()
                versions = list(
                    (
                        await session.execute(
                            select(DocumentVersionModel)
                            .where(
                                DocumentVersionModel.tenant_id == context.tenant_id,
                                DocumentVersionModel.document_id == document_id,
                            )
                            .order_by(DocumentVersionModel.created_at.desc())
                        )
                    ).scalars()
                )
                jobs = list(
                    (
                        await session.execute(
                            select(IngestionJobModel)
                            .where(
                                IngestionJobModel.tenant_id == context.tenant_id,
                                IngestionJobModel.document_id == document_id,
                            )
                            .order_by(IngestionJobModel.created_at.desc())
                        )
                    ).scalars()
                )
                current_version = next(
                    (item for item in versions if item.id == authorized.version_id),
                    None,
                )
                latest_version = versions[0] if versions else None
                latest_job = jobs[0] if jobs else None
                artifacts = list(
                    (
                        await session.execute(
                            select(VersionArtifactModel).where(
                                VersionArtifactModel.tenant_id == context.tenant_id,
                                VersionArtifactModel.document_id == document_id,
                                VersionArtifactModel.version_id == authorized.version_id,
                            )
                        )
                    ).scalars()
                )
                generation = (
                    await session.execute(
                        select(DenseGenerationModel)
                        .where(
                            DenseGenerationModel.tenant_id == context.tenant_id,
                            DenseGenerationModel.document_id == document_id,
                            DenseGenerationModel.version_id == authorized.version_id,
                            DenseGenerationModel.status == "active",
                        )
                        .order_by(DenseGenerationModel.created_at.desc())
                    )
                ).scalars().first()
        except ResourceNotFound as exc:
            raise HTTPException(
                status_code=404,
                detail={"kind": "resource_not_found", "message": "资源不存在"},
            ) from exc

        def version_payload(version: Any | None) -> ProductVersionStatusResponse | None:
            """把 ORM 版本映射为不包含 content hash 的安全响应。"""

            if version is None:
                return None
            return ProductVersionStatusResponse(
                version_id=version.id,
                status=version.status.value,
                created_at=version.created_at,
                published_at=version.published_at,
            )

        return ProductDocumentStatusResponse(
            document_id=document.id,
            document_status=document.status.value,
            current_version=version_payload(current_version),
            latest_version=version_payload(latest_version),
            latest_job=(
                ProductJobStatusResponse(
                    job_id=latest_job.id,
                    status=latest_job.status.value,
                    attempt=latest_job.attempt,
                    created_at=latest_job.created_at,
                    updated_at=latest_job.updated_at,
                )
                if latest_job is not None
                else None
            ),
            generation=(
                ProductGenerationStatusResponse(
                    generation_id=generation.generation_id,
                    status=generation.status.value,
                    expected_item_count=generation.expected_item_count,
                    actual_item_count=generation.actual_item_count,
                    checkpoint=generation.checkpoint,
                )
                if generation is not None
                else None
            ),
            artifacts={
                kind.value: ProductArtifactStatusResponse(
                    status=next(
                        (
                            artifact.status.value
                            for artifact in artifacts
                            if artifact.kind == kind
                        ),
                        "pending",
                    ),
                    item_count=next(
                        (
                            artifact.item_count
                            for artifact in artifacts
                            if artifact.kind == kind
                        ),
                        0,
                    ),
                )
                for kind in ArtifactKind
            },
        )

    @app.put(
        "/api/v1/documents/{document_id}/acl/{principal_type}/{principal_id}",
        response_model=ProductACLGrantResponse,
    )
    async def grant_document_acl(
        document_id: str,
        principal_type: ACLPrincipalType,
        principal_id: str,
        context=Depends(product_context_dependency),  # noqa: B008
    ) -> ProductACLGrantResponse:
        """由同 tenant admin 幂等授予 USER/ROLE READ ACL。"""

        database_dependency = app.state.database
        if database_dependency is None:
            raise HTTPException(
                status_code=503,
                detail={"kind": "product_unavailable", "message": "产品数据库未配置"},
            )
        try:
            async with database_dependency.transaction() as session:
                await app.state.authorization_service.grant_read(
                    session,
                    context,
                    document_id=document_id,
                    principal_type=principal_type,
                    principal_id=principal_id,
                )
        except AuthorizationDenied as exc:
            raise HTTPException(
                status_code=403,
                detail={"kind": "forbidden", "message": "当前身份无权管理 ACL"},
            ) from exc
        except ResourceNotFound as exc:
            raise HTTPException(
                status_code=404,
                detail={"kind": "resource_not_found", "message": "资源不存在"},
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"kind": "invalid_request", "message": str(exc)},
            ) from exc
        return ProductACLGrantResponse(
            document_id=document_id,
            principal_type=principal_type.value,
            principal_id=principal_id.strip(),
            permission="read",
            granted=True,
        )

    @app.delete(
        "/api/v1/documents/{document_id}/acl/{principal_type}/{principal_id}",
        response_model=ProductACLRevokeResponse,
    )
    async def revoke_document_acl(
        document_id: str,
        principal_type: ACLPrincipalType,
        principal_id: str,
        context=Depends(product_context_dependency),  # noqa: B008
    ) -> ProductACLRevokeResponse:
        """由同 tenant admin 幂等撤销 USER/ROLE READ ACL。"""

        database_dependency = app.state.database
        if database_dependency is None:
            raise HTTPException(
                status_code=503,
                detail={"kind": "product_unavailable", "message": "产品数据库未配置"},
            )
        try:
            async with database_dependency.transaction() as session:
                revoked = await app.state.authorization_service.revoke_read(
                    session,
                    context,
                    document_id=document_id,
                    principal_type=principal_type,
                    principal_id=principal_id,
                )
        except AuthorizationDenied as exc:
            raise HTTPException(
                status_code=403,
                detail={"kind": "forbidden", "message": "当前身份无权管理 ACL"},
            ) from exc
        except ResourceNotFound as exc:
            raise HTTPException(
                status_code=404,
                detail={"kind": "resource_not_found", "message": "资源不存在"},
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"kind": "invalid_request", "message": str(exc)},
            ) from exc
        return ProductACLRevokeResponse(
            document_id=document_id,
            principal_type=principal_type.value,
            principal_id=principal_id.strip(),
            permission="read",
            revoked=revoked,
        )

    @app.get("/api/v1/meta")
    async def meta() -> dict[str, str]:
        return {"name": "EnterpriseKnowledgeRAG", "version": "0.1.0"}

    @app.post("/api/v1/documents", response_model=ProductUploadResponse, status_code=201)
    async def upload_document(
        request: Request,
        context=Depends(product_context_dependency),  # noqa: B008
        file: UploadFile = File(...),  # noqa: B008
        source_id: str = Form(...),
        title: str = Form(...),
        idempotency_key: str = Form(...),
    ) -> ProductUploadResponse:
        """接收 multipart 文档并交给同步产品编排服务。"""

        service = app.state.product_ingestion_service
        if service is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "kind": "product_unavailable",
                    "message": "产品摄取服务未配置",
                },
            )

        # API 入口也执行一次角色门禁，避免测试替身或未来替换的编排服务遗漏
        # 上传权限检查；领域服务仍会再次校验，形成纵深防御。
        try:
            AuthorizationService().require_any_role(context, {"editor", "admin"})
        except AuthorizationDenied as exc:
            raise HTTPException(
                status_code=403,
                detail={
                    "kind": "forbidden",
                    "message": "当前身份无权上传文档",
                },
            ) from exc

        # FastAPI 默认会忽略未知 multipart 字段；产品入口必须显式拒绝 tenant/actor
        # 等客户端注入字段，确保身份只能来自已验证 Bearer token。
        form = await request.form()
        allowed_fields = {"file", "source_id", "title", "idempotency_key"}
        if set(form.keys()) - allowed_fields:
            raise HTTPException(
                status_code=422,
                detail={
                    "kind": "invalid_request",
                    "message": "请求包含不支持的字段",
                },
            )

        content = await file.read()
        try:
            result = await service.upload(
                context,
                source_id=source_id,
                title=title,
                filename=file.filename or "upload.bin",
                content=content,
                content_type=file.content_type,
                idempotency_key=idempotency_key,
            )
        except AuthorizationDenied as exc:
            raise HTTPException(
                status_code=403,
                detail={
                    "kind": "forbidden",
                    "message": "当前身份无权上传文档",
                },
            ) from exc
        except IdempotencyConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "kind": "idempotency_conflict",
                    "message": "幂等键对应的内容不一致",
                },
            ) from exc
        except IngestionValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "kind": "invalid_document",
                    "message": str(exc),
                },
            ) from exc
        except (ProductIndexingError, IngestionServiceError) as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "kind": "product_unavailable",
                    "message": "文档同步摄取或索引发布失败",
                },
            ) from exc
        return ProductUploadResponse.from_result(result)

    @app.post("/api/v1/rag/query", response_model=ProductRagQueryResponse)
    async def product_rag_query(
        request: ProductRagQueryRequest,
        context=Depends(product_context_dependency),  # noqa: B008
    ) -> ProductRagQueryResponse | JSONResponse:
        """执行服务端授权的混合检索、摘录问答和引用复核。"""

        retrieval_service = app.state.product_retrieval_service
        database_dependency = app.state.database
        if retrieval_service is None or database_dependency is None:
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "kind": "retrieval_unavailable",
                        "message": "产品检索依赖未就绪",
                    }
                },
            )

        try:
            async with database_dependency.session() as session:
                retrieval_result = await retrieval_service.search(
                    session,
                    context,
                    question=request.question,
                    candidate_k=request.candidate_k,
                    top_k=request.top_k,
                )
        except Exception as exc:
            # 检索失败只返回稳定类别；不把 collection、SQL、上游响应或模型路径
            # 写入客户端错误体。RerankerUnavailable 由服务层转换为同一安全边界。
            from enterprise_rag.retrieval.hybrid import RerankerUnavailable

            if isinstance(exc, RerankerUnavailable):
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": {
                            "kind": "reranker_unavailable",
                            "message": "Reranker 不可用",
                        }
                    },
                )
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "kind": "retrieval_unavailable",
                        "message": "产品检索依赖未就绪",
                    }
                },
            )

        if not retrieval_result.candidates:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "kind": "evidence_not_found",
                        "message": "未检索到可用于回答的证据",
                    }
                },
            )

        # 只把服务端已经完成授权和 Parent 去重的证据转换为 F2-G evidence；请求体
        # 中没有 tenant、version 或 evidence 字段，调用方无法越过这一步。
        candidate_by_evidence_id = {}
        evidence = []
        for index, candidate in enumerate(retrieval_result.candidates, start=1):
            evidence_id = f"retrieved-{index}"
            candidate_by_evidence_id[evidence_id] = candidate
            evidence.append(
                EvidenceRequest(
                    evidence_id=evidence_id,
                    text=candidate.text,
                    source=f"{candidate.result.document_id}#{candidate.chunk_id}",
                )
            )

        try:
            excerpt_request = ExcerptQueryRequest(
                question=request.question,
                evidence=evidence,
                model_alias=request.model_alias,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                timeout_ms=request.timeout_ms,
            )
            response = await query(excerpt_request)
        except ValueError:
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "kind": "retrieval_unavailable",
                        "message": "产品检索依赖未就绪",
                    }
                },
            )

        if isinstance(response, JSONResponse):
            return response

        response = response.model_copy(update={"context_source": "retrieved"})
        if response.citation_validation == "passed":
            # 模型引用返回前重新打开数据库 session，复核当前版本和 ACL；这一步
            # 不信任检索阶段缓存的授权结论，撤权后旧 citation 不能继续出站。
            try:
                async with database_dependency.session() as session:
                    for citation in response.citations:
                        candidate = candidate_by_evidence_id.get(citation.evidence_id)
                        if candidate is None:
                            raise ResourceNotFound("资源不存在")
                        document = await app.state.authorization_service.require_document_read(
                            session,
                            context,
                            candidate.result.document_id,
                        )
                        if document.version_id != candidate.result.version_id:
                            raise ResourceNotFound("资源不存在")
            except ResourceNotFound:
                return JSONResponse(
                    status_code=404,
                    content={
                        "error": {
                            "kind": "evidence_not_found",
                            "message": "引用对应的资源不可见",
                        }
                    },
                )

        payload = response.model_dump()
        payload["retrieval_trace"] = ProductRetrievalTraceResponse.from_result(
            retrieval_result
        ).model_dump()
        return ProductRagQueryResponse.model_validate(payload)

    @app.post("/api/v1/query", response_model=ExcerptResponse)
    async def query(request: ExcerptQueryRequest) -> ExcerptResponse | JSONResponse:
        """执行一次有界 JSON 摘录问答，并返回可观测但不泄露正文的结果。"""

        request_id = str(uuid4())
        trace_id = str(uuid4())
        started_at = time.monotonic()
        deadline = started_at + request.timeout_ms / 1000
        queue_ms: float | None = None
        generation_started: float | None = None
        generation_ms: float | None = None
        validation_started: float | None = None
        validation_ms: float | None = None
        lease: SchedulerLease | None = None
        provider_response: ChatResponse | None = None

        try:
            registered = app.state.provider_registry.resolve(request.model_alias, "chat.complete")
            scheduler = app.state.schedulers.get(request.model_alias)
            if scheduler is None:
                raise ValueError("未注册的 model_alias")

            service = ExcerptAnswerService(registered.provider)
            prepared_request, validated_bundle = service.prepare_request(
                question=request.question,
                evidence=[item.model_dump() for item in request.evidence],
                model=app.state.settings.local_llm_model,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
            )

            remaining = _remaining_seconds(deadline)
            if remaining <= 0:
                raise TimeoutError
            # 排队最多使用 10 秒，但绝不能超过调用方声明的剩余总预算。
            lease = await scheduler.acquire(queue_timeout=min(10.0, remaining))
            queue_ms = lease.queue_ms
            try:
                remaining = _remaining_seconds(deadline)
                if remaining <= 0:
                    raise TimeoutError
                generation_started = time.monotonic()
                async with asyncio.timeout(remaining):
                    provider_response = await registered.provider.complete(prepared_request)
                generation_ms = _elapsed_ms(generation_started)
            finally:
                await lease.release()
                lease = None

            validation_started = time.monotonic()
            result = service.interpret_response(provider_response, validated_bundle)
            validation_ms = _elapsed_ms(validation_started)

            completion_ms = _elapsed_ms(started_at)
            provider_result = _provider_result_payload(
                request_id=request_id,
                provider_alias=request.model_alias,
                model=prepared_request.model,
                status="success",
                latency_ms=generation_ms,
                response=provider_response,
            )
            return ExcerptResponse(
                request_id=request_id,
                trace_id=trace_id,
                context_source=result.context_source,
                answer_status=result.answer_status,
                citation_validation=result.citation_validation,
                message=result.message,
                citations=[CitationResponse(**item) for item in result.citations],
                provider_result=provider_result,
                timings=_timings_payload(
                    queue_ms=queue_ms,
                    ttft_ms=None,
                    generation_ms=generation_ms,
                    validation_ms=validation_ms,
                    completion_ms=completion_ms,
                ),
            )
        except ProviderError as exc:
            return provider_error_response(exc)
        except TimeoutError:
            return provider_error_response(
                ProviderError(ProviderErrorKind.TIMEOUT, "请求超过总时间预算", retryable=True)
            )
        except (ExcerptValidationError, ValueError) as exc:
            # Pydantic 已经处理大部分边界；这里覆盖领域校验和 registry
            # 解析失败，统一返回 422，且不让 Python traceback 穿透 HTTP。
            return JSONResponse(
                status_code=422,
                content={"error": {"kind": "invalid_request", "message": str(exc)}},
            )
        except asyncio.CancelledError:
            # HTTP task 被取消时不能继续生成或尝试构造响应；finally 仍负责
            # 释放任何已取得的 scheduler lease。
            raise
        except Exception:
            # 未分类异常只暴露稳定 unknown；原始 traceback 交给服务端日志，
            # 不进入客户端响应，也不把模型正文当作诊断信息返回。
            return provider_error_response(
                ProviderError(ProviderErrorKind.UNKNOWN, "内部调用失败")
            )
        finally:
            if lease is not None:
                await lease.release()

    @app.post("/api/v1/retrieval/query", response_model=ExcerptResponse)
    async def retrieval_query(request: RetrievalQueryRequest) -> ExcerptResponse | JSONResponse:
        """检索指定 tenant/version 的 Parent 证据，并复用 F2-G 完成摘录问答。"""

        database_dependency = app.state.database
        embedding_dependency = app.state.embedding_provider
        repository_dependency = app.state.vector_repository
        if (
            database_dependency is None
            or embedding_dependency is None
            or repository_dependency is None
        ):
            return JSONResponse(
                status_code=503,
                content={
                    "error": {"kind": "retrieval_unavailable", "message": "检索依赖未就绪"}
                },
            )

        try:
            # 查询只生成一次 embedding；Repository 必须在 SQL 中先限定 tenant/version，
            # 当前 P0 再在应用层计算 cosine，不能把它表述为 pgvector 检索。
            query_vectors = await embedding_dependency.embed([request.question])
            if len(query_vectors) != 1 or not query_vectors[0]:
                raise ValueError("查询 embedding 响应数量不正确")
            query_vector = query_vectors[0]
            async with database_dependency.session() as session:
                retrieval_results = await repository_dependency.search(
                    session,
                    request.tenant_id,
                    request.version_id,
                    query_vector,
                    request.top_k,
                )
        except Exception:
            # Embedding 或数据库异常不能把内部连接信息、SQL 或上游响应泄露给调用方。
            return JSONResponse(
                status_code=503,
                content={
                    "error": {"kind": "retrieval_unavailable", "message": "检索依赖未就绪"}
                },
            )

        # Repository 返回 child 命中；相同 Parent 只保留最高分 child。没有 Parent 的
        # child 使用自身身份去重，避免不同独立 child 被错误合并。
        unique_results = []
        seen_evidence: set[tuple[str, str]] = set()
        for result in sorted(retrieval_results, key=lambda item: item.score, reverse=True):
            identity = (
                ("parent", result.parent_id)
                if result.parent_id
                else ("chunk", result.chunk_id)
            )
            if identity in seen_evidence:
                continue
            seen_evidence.add(identity)
            unique_results.append(result)

        if not unique_results:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "kind": "evidence_not_found",
                        "message": "未检索到可用于回答的证据",
                    }
                },
            )

        try:
            # 将检索结果转换成现有 F2-G evidence 契约，继续复用 Prompt、模型调度、
            # 引用 identity 校验和稳定错误响应，不复制第二套回答逻辑。
            excerpt_request = ExcerptQueryRequest(
                question=request.question,
                evidence=[
                    EvidenceRequest(
                        evidence_id=f"retrieved-{index}",
                        text=result.text,
                        source=f"{result.document_id}#{result.chunk_id}",
                    )
                    for index, result in enumerate(unique_results, start=1)
                ],
                model_alias=request.model_alias,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                timeout_ms=request.timeout_ms,
            )
        except ValueError:
            return JSONResponse(
                status_code=503,
                content={
                    "error": {"kind": "retrieval_unavailable", "message": "检索依赖未就绪"}
                },
            )

        response = await query(excerpt_request)
        if isinstance(response, ExcerptResponse):
            return response.model_copy(update={"context_source": "retrieved"})
        return response

    @app.post("/api/v1/query/stream", response_model=None)
    async def query_stream(
        query: ExcerptQueryRequest,
        http_request: Request,
    ) -> StreamingResponse | JSONResponse:
        """消费 Provider 增量流，并在完整正文形成后执行引用 identity 校验。"""

        request_id = str(uuid4())
        trace_id = str(uuid4())
        started_at = time.monotonic()
        deadline = started_at + query.timeout_ms / 1000
        service: ExcerptAnswerService
        registered: RegisteredProvider
        stream_mode = "stream"
        lease: SchedulerLease | None = None

        try:
            try:
                # 正常路径必须显式声明 chat.stream capability。
                registered = app.state.provider_registry.resolve(query.model_alias, "chat.stream")
            except ValueError:
                # 兼容只注册 complete 的确定性 Fake：将完整响应作为一个
                # 单块 token 发送，仍然走同一最终解析/引用校验路径。真实
                # LM Studio 默认同时注册 stream，因此不会退化。
                registered = app.state.provider_registry.resolve(query.model_alias, "chat.complete")
                stream_mode = "complete_fallback"

            scheduler = app.state.schedulers.get(query.model_alias)
            if scheduler is None:
                raise ValueError("未注册的 model_alias")
            service = ExcerptAnswerService(registered.provider)
            prepared_request, validated_bundle = service.prepare_request(
                question=query.question,
                evidence=[item.model_dump() for item in query.evidence],
                model=app.state.settings.local_llm_model,
                temperature=query.temperature,
                max_tokens=query.max_tokens,
            )

            remaining = _remaining_seconds(deadline)
            if remaining <= 0:
                raise TimeoutError
            # 先在 StreamingResponse 创建前取得 lease。队列满或排队超时因此
            # 仍能返回普通 JSON 429，而不会把准入错误伪装成 SSE 事件。
            lease = await scheduler.acquire(queue_timeout=min(10.0, remaining))
        except ProviderError as exc:
            return provider_error_response(exc)
        except TimeoutError:
            return provider_error_response(
                ProviderError(ProviderErrorKind.TIMEOUT, "请求超过总时间预算", retryable=True)
            )
        except (ExcerptValidationError, ValueError) as exc:
            return JSONResponse(
                status_code=422,
                content={"error": {"kind": "invalid_request", "message": str(exc)}},
            )

        async def events():
            """SSE 生成器：lease 覆盖整个流，所有终态只允许出现一次。"""

            done_emitted = False
            seq = 0
            stream_iterator: Any = None
            content_parts: list[str] = []
            finish_reason: str | None = None
            usage: ChatUsage | None = None
            provider_response: ChatResponse | None = None
            provider_started: float | None = None
            generation_ms: float | None = None
            validation_started: float | None = None
            validation_ms: float | None = None
            first_visible_at: float | None = None
            structured_done = stream_mode != "stream"
            stream_sanitizer = VisibleStreamSanitizer()

            def emit(event_type: str, **payload: object) -> str:
                """编码单个业务事件；序号只在实际准备发送时递增。"""

                nonlocal seq
                seq += 1
                return format_sse_event(
                    {
                        "type": event_type,
                        "request_id": request_id,
                        "seq": seq,
                        **payload,
                    }
                )

            def emit_done(
                *,
                answer_status: str,
                citation_validation: str,
                message: str,
                response: ChatResponse | None = None,
                error: ProviderError | None = None,
                status: str | None = None,
            ) -> str:
                """生成唯一 done；done 不重复传输模型全文。"""

                nonlocal done_emitted
                if done_emitted:
                    return ""
                done_emitted = True
                effective_status = status or ("partial" if content_parts else "failure")
                provider_result = _provider_result_payload(
                    request_id=request_id,
                    provider_alias=query.model_alias,
                    model=(response.model if response is not None else prepared_request.model),
                    status=effective_status,
                    latency_ms=generation_ms,
                    response=response,
                    error=error,
                )
                return emit(
                    "done",
                    trace_id=trace_id,
                    answer_status=answer_status,
                    citation_validation=citation_validation,
                    message=message,
                    provider_result=provider_result.model_dump(),
                    timings=_timings_payload(
                        queue_ms=lease.queue_ms,
                        ttft_ms=(
                            max(0.0, (first_visible_at - started_at) * 1000)
                            if first_visible_at is not None
                            else None
                        ),
                        generation_ms=generation_ms,
                        validation_ms=validation_ms,
                        completion_ms=_elapsed_ms(started_at),
                    ).model_dump(),
                )

            async def ensure_connected() -> None:
                """在继续向客户端写事件前检查断开和总 deadline。"""

                if await http_request.is_disconnected():
                    raise asyncio.CancelledError
                if _remaining_seconds(deadline) <= 0:
                    raise TimeoutError

            def collect_visible(raw_text: str) -> str:
                """过滤 reasoning 包装并累计真正允许发送的正文增量。"""

                nonlocal first_visible_at
                visible = stream_sanitizer.push(raw_text)
                if visible:
                    if first_visible_at is None:
                        first_visible_at = time.monotonic()
                    content_parts.append(visible)
                return visible

            try:
                remaining = _remaining_seconds(deadline)
                if remaining <= 0:
                    raise TimeoutError
                async with asyncio.timeout(remaining):
                    await ensure_connected()
                    yield emit("stage", stage="validated")
                    await ensure_connected()
                    yield emit("stage", stage="queued")
                    await ensure_connected()
                    yield emit("stage", stage="generating")

                    content_parts = []
                    provider_started = time.monotonic()

                    if stream_mode == "complete_fallback":
                        # 仅针对不具备 stream capability 的确定性 Fake；仍然
                        # 保持 lease，避免 complete 调用绕过并发和队列边界。
                        provider_response = await registered.provider.complete(prepared_request)
                        if not isinstance(provider_response, ChatResponse):
                            raise ProviderError(
                                ProviderErrorKind.PROTOCOL,
                                "Provider 完整响应类型非法",
                            )
                        finish_reason = provider_response.finish_reason
                        usage = provider_response.usage
                        visible = collect_visible(provider_response.content)
                        if visible:
                            await ensure_connected()
                            yield emit("token", text=visible)
                        structured_done = True
                    else:
                        # 真实 adapter 优先提供结构化事件；没有该方法时才
                        # 兼容旧式 stream() 字符串接口。这个顺序也避免把
                        # StructuredStreamingProvider 意外降级成旧接口。
                        stream_events = getattr(registered.provider, "stream_events", None)
                        if callable(stream_events):
                            stream_iterator = await _await_maybe(stream_events(prepared_request))
                            async for event in stream_iterator:
                                if not isinstance(event, ChatStreamEvent):
                                    raise ProviderError(
                                        ProviderErrorKind.PROTOCOL,
                                        "Provider 结构化流事件类型非法",
                                    )
                                if event.finish_reason is not None:
                                    finish_reason = event.finish_reason
                                if event.usage is not None:
                                    usage = event.usage
                                if event.done:
                                    structured_done = True
                                    break
                                if not isinstance(event.text, str):
                                    raise ProviderError(
                                        ProviderErrorKind.PROTOCOL,
                                        "Provider 流正文必须是字符串",
                                    )
                                if not event.text:
                                    continue
                                visible = collect_visible(event.text)
                                if not visible:
                                    continue
                                await ensure_connected()
                                yield emit("token", text=visible)
                        else:
                            stream = getattr(registered.provider, "stream", None)
                            if not callable(stream):
                                raise ProviderError(
                                    ProviderErrorKind.PROTOCOL,
                                    "Provider 不支持流式接口",
                                )
                            stream_iterator = await _await_maybe(stream(prepared_request))
                            async for chunk in stream_iterator:
                                if not isinstance(chunk, str):
                                    raise ProviderError(
                                        ProviderErrorKind.PROTOCOL,
                                        "Provider 流增量必须是字符串",
                                    )
                                if not chunk:
                                    continue
                                visible = collect_visible(chunk)
                                if not visible:
                                    continue
                                await ensure_connected()
                                yield emit("token", text=visible)
                            # 旧式 stream 没有显式 done 元数据；迭代器正常结束
                            # 即表示其可观察的生成阶段结束，但 finish_reason/usage
                            # 仍保持 null，不能凭空补成 stop。
                            structured_done = True

                    # Provider 的终态事件可能正好位于 reasoning/围栏之后，
                    # 过滤器需要一次显式 finish 才能把跨 chunk 的最后可见尾巴
                    # 交给客户端；若包装未闭合，这里会进入统一 protocol 失败路径。
                    tail = stream_sanitizer.finish()
                    if tail:
                        # finish 返回的尾巴尚未经过 collect_visible，因此要
                        # 先写入同一 content_parts，确保最终 JSON 校验看到
                        # 完整正文，而不是只看到已经提前发送的前缀。
                        if first_visible_at is None:
                            first_visible_at = time.monotonic()
                        content_parts.append(tail)
                        await ensure_connected()
                        yield emit("token", text=tail)

                    generation_ms = _elapsed_ms(provider_started)
                    provider_response = provider_response or ChatResponse(
                        content="".join(content_parts),
                        model=prepared_request.model,
                        finish_reason=finish_reason,
                        usage=usage,
                    )
                    provider_response = ChatResponse(
                        content="".join(content_parts),
                        model=provider_response.model,
                        finish_reason=finish_reason or provider_response.finish_reason,
                        usage=usage or provider_response.usage,
                    )

                    if stream_mode == "stream" and not structured_done:
                        raise ProviderError(
                            ProviderErrorKind.PROTOCOL,
                            "Provider 流未收到终态事件",
                        )
                    if not content_parts:
                        raise ProviderError(
                            ProviderErrorKind.PROTOCOL,
                            "Provider 流未返回内容",
                        )

                    await ensure_connected()
                    yield emit("stage", stage="validating_citations")
                    validation_started = time.monotonic()
                    result = service.interpret_response(provider_response, validated_bundle)
                    validation_ms = _elapsed_ms(validation_started)

                    if result.answer_status == "invalid_citations":
                        # token 已经发出，不能撤回；显式 error 告知客户端最终
                        # identity 校验失败，避免消费者误把增量当成成功答案。
                        await ensure_connected()
                        yield emit("error", error_kind="invalid_citations")
                    for citation in result.citations:
                        await ensure_connected()
                        yield emit("citation", citation=citation)
                    await ensure_connected()
                    yield emit_done(
                        answer_status=result.answer_status,
                        citation_validation=result.citation_validation,
                        message=result.message,
                        response=provider_response,
                        status="success",
                    )
            except asyncio.CancelledError:
                # 客户端断开或请求 task 被取消时不再尝试发送事件；finally
                # 会关闭迭代器并释放 lease。没有 done 的流由客户端按未完成处理。
                raise
            except TimeoutError:
                # deadline 已经消耗的情况下，不为了发送终态重新获得预算；若
                # 恰好仍有极短可写窗口，则最多发送一组 error + done。
                provider_error = ProviderError(
                    ProviderErrorKind.TIMEOUT,
                    "请求超过总时间预算",
                    retryable=True,
                )
                provider_status = "partial" if content_parts else "failure"
                if _remaining_seconds(deadline) > 0:
                    yield emit("error", error_kind=provider_error.kind.value)
                    yield emit_done(
                        answer_status="failed",
                        citation_validation="not_checked",
                        message=_safe_error_message(provider_error.kind),
                        response=provider_response,
                        error=provider_error,
                        status=provider_status,
                    )
            except ProviderError as exc:
                if exc.kind is ProviderErrorKind.CANCELLED:
                    provider_status = "cancelled"
                    answer_status = "cancelled"
                else:
                    provider_status = "partial" if content_parts else "failure"
                    answer_status = "failed"
                if _remaining_seconds(deadline) > 0:
                    yield emit("error", error_kind=exc.kind.value)
                    yield emit_done(
                        answer_status=answer_status,
                        citation_validation="not_checked",
                        message=_safe_error_message(exc.kind),
                        response=provider_response,
                        error=exc,
                        status=provider_status,
                    )
            except Exception:
                # 未分类异常只暴露 unknown；原始异常和模型正文不进入 SSE。
                provider_error = ProviderError(ProviderErrorKind.UNKNOWN, "内部调用失败")
                provider_status = "partial" if content_parts else "failure"
                if _remaining_seconds(deadline) > 0:
                    yield emit("error", error_kind=provider_error.kind.value)
                    yield emit_done(
                        answer_status="failed",
                        citation_validation="not_checked",
                        message=_safe_error_message(provider_error.kind),
                        response=provider_response,
                        error=provider_error,
                        status=provider_status,
                    )
            finally:
                # 上游 aclose() 自身也可能在取消竞态中抛出 CancelledError。
                # 清理异常必须继续向上传播，但不能因此跳过 lease 释放；
                # 使用嵌套 finally 保证 scheduler 的 active 计数恰好归零。
                try:
                    await _close_async_iterator(stream_iterator)
                finally:
                    await lease.release()

        return StreamingResponse(events(), media_type="text/event-stream")

    return app


app = create_app()
