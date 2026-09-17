"""Weaviate BM25F transport 的 schema、权重和过滤契约测试。"""

from __future__ import annotations

import json

import httpx
import pytest

from enterprise_rag.retrieval.sparse import SparseObject, SparseProfile
from enterprise_rag.retrieval.weaviate_sparse import WeaviateBM25FTransport


def _profile() -> SparseProfile:
    """返回真实 smoke 使用的固定 BM25F 字段权重。"""

    return SparseProfile(
        tokenizer_version="tokenizer-v1",
        preprocessing_version="preprocess-v1",
        schema_version="sparse-v1",
        field_weights={
            "title": 2.0,
            "section_title": 2.0,
            "api_symbols": 3.0,
            "body": 1.0,
        },
    )


def _object() -> SparseObject:
    """返回包含 BM25F 字段和完整 scope 的最小 sparse object。"""

    return SparseObject(
        object_id="sparse-object-1",
        properties={
            "tenant_id": "tenant-a",
            "document_id": "doc-1",
            "version_id": "version-1",
            "generation_id": "generation-1",
            "sparse_profile": _profile().key,
            "manifest_hash": "manifest-hash",
            "chunk_id": "child-1",
            "parent_id": "parent-1",
            "title": "账号",
            "section_title": "账号 > 创建用户",
            "api_symbols": "CreateUser ERR_401 /v1/users",
            "body": "调用 CreateUser API 返回 ERR_401。",
            "heading_path": ["账号", "创建用户"],
            "source_locators": [{"line": 3}],
        },
    )


@pytest.mark.asyncio
async def test_schema_and_sparse_batch_preserve_bm25f_fields() -> None:
    """真实 schema 必须声明四个字段，batch 必须保留 projector 属性。"""

    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        """模拟 schema 不存在后创建，并接受一条 sparse object。"""

        requests.append(request)
        if request.method == "GET":
            return httpx.Response(404, request=request)
        if request.url.path == "/v1/schema":
            payload = json.loads(request.content)
            properties = {item["name"]: item for item in payload["properties"]}
            assert payload["vectorizer"] == "none"
            assert {
                "title",
                "section_title",
                "api_symbols",
                "body",
            } <= set(properties)
            assert properties["api_symbols"]["tokenization"] == "word"
            return httpx.Response(200, json=payload, request=request)
        if request.url.path == "/v1/batch/objects":
            item = json.loads(request.content)["objects"][0]
            assert item["properties"]["tenant_id"] == "tenant-a"
            assert item["properties"]["api_symbols"].startswith("CreateUser")
            return httpx.Response(
                200,
                json=[{"id": item["id"], "result": {"status": "SUCCESS"}}],
                request=request,
            )
        raise AssertionError(f"unexpected path: {request.url.path}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = WeaviateBM25FTransport(client, "http://weaviate.test")
        await transport.ensure_collection("SparseTenant_demo", "sparse-v1")
        accepted = await transport.upsert_batch("SparseTenant_demo", [_object()])

    assert accepted == 1


@pytest.mark.asyncio
async def test_schema_uses_explicit_tokenization_for_all_bm25f_fields() -> None:
    """隔离实验指定的 tokenizer 必须一致应用到全部 BM25F 字段。"""

    observed_tokenizations: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        """模拟 collection 不存在，并记录创建 schema 中的 tokenizer。"""

        if request.method == "GET":
            return httpx.Response(404, request=request)
        payload = json.loads(request.content)
        for item in payload["properties"]:
            if item["name"] in {"title", "section_title", "api_symbols", "body"}:
                observed_tokenizations[item["name"]] = item["tokenization"]
        return httpx.Response(200, json=payload, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = WeaviateBM25FTransport(client, "http://weaviate.test")
        await transport.ensure_collection(
            "SparseTenant_trigram",
            "sparse-trigram-v1",
            tokenization="trigram",
        )

    assert observed_tokenizations == {
        "title": "trigram",
        "section_title": "trigram",
        "api_symbols": "trigram",
        "body": "trigram",
    }


@pytest.mark.asyncio
async def test_bm25f_query_uses_weights_and_tenant_version_filter() -> None:
    """查询必须使用字段权重、scope 过滤，并规避 Int variable 缺陷。"""

    observed_payload: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        """记录 GraphQL 请求并返回目标 chunk。"""

        observed_payload.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "data": {
                    "Get": {
                        "SparseTenant_demo": [
                            {
                                "tenant_id": "tenant-a",
                                "version_id": "version-1",
                                "generation_id": "generation-1",
                                "chunk_id": "child-1",
                                "_additional": {"score": "3.25"},
                            }
                        ]
                    }
                }
            },
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = WeaviateBM25FTransport(client, "http://weaviate.test")
        rows = await transport.search(
            "SparseTenant_demo",
            profile=_profile(),
            tenant_id="tenant-a",
            generation_id="generation-1",
            version_id="version-1",
            query="CreateUser",
            top_k=2,
        )

    query_text = str(observed_payload["query"])
    variables = observed_payload["variables"]
    assert '"title^2"' in query_text
    assert '"api_symbols^3"' in query_text
    assert "GetObjectsSparseTenant_demoWhereInpObj!" in query_text
    assert query_text.count("{") == query_text.count("}")
    assert "$limit" not in query_text
    assert "limit: 2" in query_text
    assert variables["query"] == "CreateUser"
    assert variables["where"]["operator"] == "And"
    assert [item["valueText"] for item in variables["where"]["operands"]] == [
        "tenant-a",
        "generation-1",
        "version-1",
    ]
    assert rows[0]["chunk_id"] == "child-1"
