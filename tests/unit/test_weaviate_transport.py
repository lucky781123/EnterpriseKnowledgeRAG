"""Weaviate HTTPX transport 的协议、幂等和错误边界测试。"""

from __future__ import annotations

import httpx
import pytest
import json

from enterprise_rag.retrieval.contracts import WeaviateObject
from enterprise_rag.retrieval.weaviate import (
    WeaviateHTTPXTransport,
    WeaviateTransportError,
)


def _object() -> WeaviateObject:
    """返回一个带稳定 chunk identity 的最小 Dense 对象。"""

    return WeaviateObject(
        object_id="chunk-1",
        vector=(0.1, 0.2),
        properties={
            "tenant_id": "tenant-a",
            "document_id": "doc-1",
            "version_id": "version-1",
            "generation_id": "generation-1",
            "index_profile": "demo:2:chunk:schema",
            "chunk_id": "chunk-1",
            "parent_id": "parent-1",
            "text": "正文",
            "parent_text": "父正文",
            "heading_path": ["标题"],
            "source_locators": [{"line": 1}],
        },
    )


@pytest.mark.asyncio
async def test_collection_schema_and_batch_upsert_are_stable():
    """collection schema 固定，重复 upsert 必须使用相同 object identity。"""

    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(404, request=request)
        if request.url.path == "/v1/schema":
            payload = json.loads(request.content)
            assert payload["class"] == "DenseTenant_demo"
            assert payload["vectorizer"] == "none"
            assert {item["name"] for item in payload["properties"]} >= {
                "generation_id",
                "version_id",
                "chunk_id",
                "text",
            }
            return httpx.Response(200, json=payload, request=request)
        if request.url.path == "/v1/batch/objects":
            objects = json.loads(request.content)["objects"]
            return httpx.Response(
                200,
                json=[{"id": item["id"], "result": {"status": "SUCCESS"}} for item in objects],
                request=request,
            )
        raise AssertionError(f"unexpected path: {request.url.path}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = WeaviateHTTPXTransport(client, "http://weaviate.test", api_key="secret-key")
        await transport.ensure_collection("DenseTenant_demo", "dense-v1")
        first = await transport.upsert_batch("DenseTenant_demo", [_object()])
        second = await transport.upsert_batch("DenseTenant_demo", [_object()])

    assert first.accepted == second.accepted == 1
    batch_requests = [request for request in requests if request.url.path == "/v1/batch/objects"]
    assert json.loads(batch_requests[0].content)["objects"][0]["id"] == json.loads(
        batch_requests[1].content
    )["objects"][0]["id"]


@pytest.mark.asyncio
async def test_partial_batch_failure_is_retryable_without_response_body():
    """批次部分失败应抛出可重试错误，且不能把上游 body 透出。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {"id": "ok", "result": {"status": "SUCCESS"}},
                {"id": "bad", "result": {"status": "FAILED", "error": "provider-secret-body"}},
            ],
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = WeaviateHTTPXTransport(client, "http://weaviate.test", api_key="secret-key")
        with pytest.raises(WeaviateTransportError) as captured:
            await transport.upsert_batch("DenseTenant_demo", [_object()])

    assert captured.value.retryable is True
    assert "provider-secret-body" not in str(captured.value)
    assert "secret-key" not in str(captured.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 429, 500])
async def test_http_errors_have_fixed_safe_messages(status_code: int):
    """认证、限流和服务端错误均不得回显 URL、header 或 response body。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            text="sensitive-response-body",
            headers={"x-upstream-secret": "header-secret"},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = WeaviateHTTPXTransport(client, "http://weaviate.test", api_key="secret-key")
        with pytest.raises(WeaviateTransportError) as captured:
            await transport.ensure_collection("DenseTenant_demo", "dense-v1")

    assert "sensitive-response-body" not in str(captured.value)
    assert "header-secret" not in str(captured.value)
    assert "secret-key" not in str(captured.value)
    assert captured.value.retryable is (status_code in {429, 500})


@pytest.mark.asyncio
async def test_ensure_collection_only_creates_after_not_found():
    """只有 HTTP 404 才允许进入 collection 创建分支。"""

    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(400, text="bad schema", request=request)
        raise AssertionError("400 不应继续发送创建请求")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = WeaviateHTTPXTransport(client, "http://weaviate.test")
        with pytest.raises(WeaviateTransportError) as captured:
            await transport.ensure_collection("DenseTenant_demo", "dense-v1")

    assert captured.value.kind == "invalid_request"
    assert [request.method for request in requests] == ["GET"]


@pytest.mark.asyncio
async def test_list_objects_encodes_query_parameters_and_parses_response():
    """对象列表请求应使用 HTTPX 可用的查询字符串编码。"""

    observed: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(
            200,
            json={
                "objects": [
                    {"properties": {"chunk_id": "chunk-1"}},
                ]
            },
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = WeaviateHTTPXTransport(client, "http://weaviate.test")
        chunk_ids = await transport.list_chunk_ids(
            "DenseTenant_demo", generation_id="generation/1", version_id="version 1"
        )

    assert chunk_ids == {"chunk-1"}
    assert observed[0].url.path == "/v1/objects"
    assert observed[0].url.params["class"] == "DenseTenant_demo"
    assert "generation/1" in observed[0].url.params["where"]
    assert "version 1" in observed[0].url.params["where"]
