"""Ollama Embedding API 的最小异步适配器。"""

from __future__ import annotations

from collections.abc import Sequence

import httpx


class OllamaEmbeddingProvider:
    """调用 Ollama ``/api/embed``，并统一校验返回向量。"""

    def __init__(self, client: httpx.AsyncClient, base_url: str, model: str) -> None:
        """保存共享 HTTP client、服务地址和模型名称。"""

        self.client = client
        self.base_url = base_url.rstrip("/")
        self.model = model

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """批量生成 embedding；HTTP 或协议错误统一转换为稳定异常。"""

        if not texts or any(not text.strip() for text in texts):
            raise ValueError("texts 不能为空")
        response = await self.client.post(
            f"{self.base_url}/api/embed",
            json={"model": self.model, "input": list(texts)},
            timeout=30.0,
        )
        response.raise_for_status()
        payload = response.json()
        vectors = payload.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise ValueError("Ollama embedding 响应数量不正确")
        normalized = [[float(value) for value in vector] for vector in vectors]
        self.validate_dimension(normalized)
        return normalized

    @staticmethod
    def validate_dimension(vectors: Sequence[Sequence[float]]) -> int:
        """确保批次内所有向量维度一致，并返回维度。"""

        if not vectors or not vectors[0]:
            raise ValueError("embedding 维度不能为空")
        dimension = len(vectors[0])
        if any(len(vector) != dimension for vector in vectors):
            raise ValueError("embedding 维度不一致")
        return dimension
