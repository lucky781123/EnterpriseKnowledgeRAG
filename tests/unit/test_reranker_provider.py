import pytest

from enterprise_rag.reranking.adapters import SyncRerankerAdapter
from enterprise_rag.reranking.protocols import RerankRequest


class FakeSyncReranker:
    def rerank(self, query, documents, top_k=None):
        results = [
            {"index": index, "text": text, "score": float(len(text))}
            for index, text in enumerate(documents)
        ]
        results.sort(key=lambda item: item["score"], reverse=True)
        return results[:top_k] if top_k else results


@pytest.mark.asyncio
async def test_adapter_normalizes_existing_sync_reranker():
    adapter = SyncRerankerAdapter(FakeSyncReranker())
    request = RerankRequest(query="企业知识库", documents=["短", "更长的证据"], top_k=1)
    results = await adapter.rerank(request)
    assert len(results) == 1
    assert results[0].index == 1
    assert results[0].text == "更长的证据"


@pytest.mark.asyncio
async def test_adapter_health_check_is_explicit():
    adapter = SyncRerankerAdapter(FakeSyncReranker())
    assert await adapter.health_check() is True
