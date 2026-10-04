"""验证不同Embedding profile隔离物理向量collection且保留旧部署映射。"""

import httpx
import pytest

from enterprise_rag.retrieval.contracts import WeaviateObject
from enterprise_rag.retrieval.weaviate import WeaviateHTTPXTransport


@pytest.mark.asyncio
async def test_profile_namespace_is_stable_and_isolated():
    """相同profile稳定映射，模型或维度变更不会访问旧物理collection。"""
    async with httpx.AsyncClient() as client:
        legacy = WeaviateHTTPXTransport(client, "http://localhost")
        first = WeaviateHTTPXTransport(client, "http://localhost", index_profile_key="embed:2560")
        same = WeaviateHTTPXTransport(client, "http://localhost", index_profile_key="embed:2560")
        other = WeaviateHTTPXTransport(client, "http://localhost", index_profile_key="embed:1024")
        assert legacy.physical_collection_name("DenseTenant_abc") == "DenseTenant_abc"
        name = first.physical_collection_name("DenseTenant_abc")
        assert name == same.physical_collection_name("DenseTenant_abc")
        assert name != other.physical_collection_name("DenseTenant_abc")
        assert name != first.physical_collection_name("DenseTenant_xyz")
        assert first.physical_collection_name(name) == name


@pytest.mark.asyncio
async def test_all_profile_operations_use_physical_collection():
    """建库/查询/完整性/删除都使用同一物理命名，不回到旧collection。"""
    requests = []
    def handler(request):
        """采集端点并返回最小合法Weaviate响应。"""
        requests.append(request)
        if request.url.path.startswith("/v1/schema/"):
            return httpx.Response(200, json={})
        if request.url.path == "/v1/objects":
            return httpx.Response(200, json={"objects": []})
        if request.url.path == "/v1/batch/objects":
            return httpx.Response(200, json={"results": {"successful": 0}})
        return httpx.Response(200, json={"data": {"Get": {physical: []}}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = WeaviateHTTPXTransport(client, "http://localhost", index_profile_key="new:2560")
        physical = transport.physical_collection_name("DenseTenant_abc")
        await transport.ensure_collection("DenseTenant_abc", "dense-v1")
        assert await transport.count_generation("DenseTenant_abc", generation_id="g", version_id="v") == 0
        assert not await transport.list_chunk_ids("DenseTenant_abc", generation_id="g", version_id="v")
        assert not await transport.search(
            "DenseTenant_abc", generation_id="g", version_id="v", query=[1, 0], top_k=1,
        )
        assert await transport.delete_generation(
            "DenseTenant_abc", generation_id="g", version_id="v",
        ) == 0
    assert all(physical.encode() in request.url.raw_path or physical.encode() in request.content
               for request in requests)


@pytest.mark.asyncio
async def test_upsert_rejects_wrong_profile_before_network():
    """对象不能写进另一个模型空间，profile mismatch不触发任何网络请求。"""
    def handler(_):
        """若发生网络调用则表明profile守卫失效。"""
        raise AssertionError("不应发送请求")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = WeaviateHTTPXTransport(client, "http://localhost", index_profile_key="new:2560")
        with pytest.raises(ValueError, match="profile"):
            await transport.upsert_batch("DenseTenant_abc", [
                WeaviateObject("test", (1.0, 0.0), {"index_profile": "old:1024"}),
            ])
