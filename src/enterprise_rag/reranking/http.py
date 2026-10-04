"""OpenAI 风格 HTTP Reranker 的异步适配器。"""

from __future__ import annotations

import math
from typing import Any

import httpx

from .protocols import RerankRequest, RerankResult


class HTTPReranker:
    """调用本地 ``/v1/rerank`` 服务并转换为统一 Reranker 契约。"""

    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        model: str,
        *,
        timeout: float = 60.0,
    ) -> None:
        """保存共享 HTTP client、服务地址、模型名和单次请求预算。"""
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def _url(self, suffix: str) -> str:
        """拼接服务端点，兼容填写根地址或带 ``/v1`` 的地址。"""
        root = self.base_url.removesuffix("/v1")
        return f"{root}/v1/{suffix}"

    async def health_check(self) -> bool:
        """通过模型列表接口确认服务可用且已提供配置模型。"""
        try:
            response = await self.client.get(self._url("models"), timeout=3.0)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return False
        models = payload.get("data") if isinstance(payload, dict) else None
        return isinstance(models, list) and any(
            isinstance(item, dict) and item.get("id") == self.model for item in models
        )

    async def rerank(self, request: RerankRequest) -> list[RerankResult]:
        """发送查询和候选正文，并严格校验索引、正文和分数。"""
        if not request.documents:
            return []
        if not request.query.strip() or request.top_k is not None and request.top_k <= 0:
            raise ValueError("Reranker query/top_k 非法")
        top_n = min(request.top_k or len(request.documents), len(request.documents))
        response = await self.client.post(
            self._url("rerank"),
            json={
                "model": self.model,
                "query": request.query,
                "documents": list(request.documents),
                "top_n": top_n,
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload: Any = response.json()
        rows = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValueError("Reranker 响应缺少 results")  # noqa: TRY004 -- 统一协议校验异常。
        if len(rows) != top_n:
            raise ValueError("Reranker 响应候选数量不一致")
        results: list[RerankResult] = []
        seen: set[int] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Reranker result 格式非法")  # noqa: TRY004 -- 统一协议校验异常。
            index = row.get("index")
            score = row.get("relevance_score")
            document = row.get("document")
            text = document.get("text") if isinstance(document, dict) else None
            if (
                type(index) is not int
                or index < 0
                or index >= len(request.documents)
                or index in seen
                or (document is not None and text != request.documents[index])
                or type(score) not in (float, int)
                or not math.isfinite(score)
            ):
                raise ValueError("Reranker 返回候选与请求不一致")
            # index是候选身份，正文从请求恢复，不能信任或引入服务端其他文档。
            seen.add(index)
            results.append(RerankResult(
                index=index, text=request.documents[index], score=float(score),
            ))
        return results
