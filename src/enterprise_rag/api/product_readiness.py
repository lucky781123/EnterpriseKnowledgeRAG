"""本地 P0 产品依赖的只读 readiness 检查。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ProductReadiness:
    """保存产品 readiness 的安全摘要；不携带 URL、secret 或异常正文。"""

    status: str
    dependencies: dict[str, str]

    def __post_init__(self) -> None:
        """限制对外状态集合，避免路由意外返回未定义的健康语义。"""

        if self.status not in {"ready", "not_ready"}:
            raise ValueError("ProductReadiness.status 必须是 ready 或 not_ready")


class ProductReadinessManager:
    """并行检查本地 P0 所需依赖，并把所有失败归一化为安全状态。"""

    def __init__(
        self,
        *,
        settings: Any,
        database: Any | None,
        provider_registry: Any,
        dev_token_codec: Any | None,
        embedding_provider: Any | None,
        product_ingestion_service: Any | None,
        product_retrieval_service: Any | None,
    ) -> None:
        """保存服务端依赖引用；检查本身只在 ``check`` 被调用时发生。"""

        self.settings = settings
        self.database = database
        self.provider_registry = provider_registry
        self.dev_token_codec = dev_token_codec
        self.embedding_provider = embedding_provider
        self.product_ingestion_service = product_ingestion_service
        self.product_retrieval_service = product_retrieval_service

    async def check(self) -> ProductReadiness:
        """并行探测全部产品依赖，并返回稳定的逐项状态摘要。"""

        ingestion = self.product_ingestion_service
        retrieval = self.product_retrieval_service
        dense_transport = getattr(ingestion, "dense_transport", None) or getattr(
            retrieval, "dense_transport", None
        )
        sparse_transport = getattr(ingestion, "sparse_transport", None) or getattr(
            retrieval, "sparse_transport", None
        )
        storage = getattr(ingestion, "storage", None)
        reranker = getattr(retrieval, "reranker", None)

        checks = {
            "database": self._database_status(),
            "weaviate_dense": self._dependency_status(
                dense_transport,
                configured=bool(getattr(self.settings, "weaviate_enabled", False)),
            ),
            "weaviate_sparse": self._dependency_status(
                sparse_transport,
                configured=bool(getattr(self.settings, "weaviate_enabled", False)),
            ),
            "ollama_embedding": self._dependency_status(
                self.embedding_provider,
                configured=self.embedding_provider is not None,
            ),
            "bge_reranker": self._dependency_status(
                reranker,
                configured=reranker is not None,
            ),
            "lmstudio": self._lmstudio_status(),
            "object_storage": self._dependency_status(
                storage,
                configured=storage is not None,
            ),
        }
        statuses = await asyncio.gather(*checks.values())
        dependencies = dict(zip(checks, statuses, strict=True))
        dependencies["dev_identity"] = (
            "ready" if self.dev_token_codec is not None else "not_configured"
        )
        dependencies["product_services"] = (
            "ready"
            if ingestion is not None and retrieval is not None
            else "not_configured"
        )
        overall = "ready" if all(status == "ready" for status in dependencies.values()) else "not_ready"
        return ProductReadiness(status=overall, dependencies=dependencies)

    async def _database_status(self) -> str:
        """检查数据库连通性和 migration schema；不在 readiness 中自动升级。"""

        if self.database is None:
            return "not_configured"
        try:
            if not await self.database.health_check():
                return "unavailable"
            readiness = await self.database.schema_readiness()
        except Exception:  # noqa: BLE001 - readiness 需要统一隐藏依赖异常
            return "unavailable"
        return "ready" if readiness.ready else readiness.status

    async def _lmstudio_status(self) -> str:
        """检查固定的 ``local_answer`` Provider，不允许请求方指定 endpoint。"""

        try:
            registered = self.provider_registry.resolve("local_answer", "chat.complete")
        except Exception:  # noqa: BLE001 - Provider 注册异常不能泄露到响应
            return "not_configured"
        return await self._dependency_status(registered.provider, configured=True)

    @staticmethod
    async def _dependency_status(resource: Any | None, *, configured: bool) -> str:
        """调用依赖自身的只读 ``health_check``，并隐藏所有异常细节。"""

        if not configured or resource is None:
            return "not_configured"
        health_check = getattr(resource, "health_check", None)
        if not callable(health_check):
            return "unavailable"
        try:
            return "ready" if bool(await health_check()) else "unavailable"
        except Exception:  # noqa: BLE001 - 健康探针失败统一归一化为 unavailable
            return "unavailable"


__all__ = ["ProductReadiness", "ProductReadinessManager"]
