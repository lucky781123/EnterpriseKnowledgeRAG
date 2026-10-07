"""Weaviate BM25F transport 的 schema、权重和过滤契约测试。"""

from __future__ import annotations

import json

import httpx
import pytest

from enterprise_rag.retrieval.sparse import SparseObject, SparseProfile
from enterprise_rag.retrieval.weaviate import WeaviateTransportError
from enterprise_rag.retrieval.weaviate_sparse import (
    WeaviateBM25FTransport,
    prepare_bm25_query,
)


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


def test_prepare_bm25_query_preserves_mixed_language_terms_and_expands_hyphens() -> None:
    """混合中英文问题必须保留原文，并补充连字符技术词的独立 token。"""

    prepared = prepare_bm25_query("Rerank为什么要用 Cross-Encoder")

    assert prepared.startswith("Rerank为什么要用 Cross-Encoder")
    assert "Rerank" in prepared.split()
    assert "Cross-Encoder" in prepared.split()
    assert "Cross" in prepared.split()
    assert "Encoder" in prepared.split()


@pytest.mark.parametrize("query", ["Cross-Encoder", "Bi-Encoder"])
def test_prepare_bm25_query_expands_common_encoder_terms(query: str) -> None:
    """Bi-Encoder 和 Cross-Encoder 都必须保留连字符形式及其组成词。"""

    prepared = prepare_bm25_query(query)

    assert query in prepared
    left, right = query.split("-", maxsplit=1)
    assert left in prepared.split()
    assert right in prepared.split()


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


@pytest.mark.asyncio
async def test_bm25f_query_preprocesses_mixed_language_technical_terms() -> None:
    """BM25F 请求应增强混合中英文技术词，但不能改变 scope 变量。"""

    observed_payload: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        """记录增强后的 query，并返回最小合法响应。"""

        observed_payload.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"data": {"Get": {"SparseTenant_demo": []}}},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = WeaviateBM25FTransport(client, "http://weaviate.test")
        await transport.search(
            "SparseTenant_demo",
            profile=_profile(),
            tenant_id="tenant-a",
            generation_id="generation-1",
            version_id="version-1",
            query="Rerank为什么要用 Cross-Encoder",
            top_k=20,
        )

    variables = observed_payload["variables"]
    query = variables["query"]
    assert isinstance(query, str)
    assert "Rerank为什么要用 Cross-Encoder" in query
    assert {"Rerank", "Cross-Encoder", "Cross", "Encoder"} <= set(query.split())
    assert variables["where"]["operands"][0]["valueText"] == "tenant-a"


@pytest.mark.asyncio
async def test_sparse_generation_inspection_and_delete_use_full_product_scope() -> None:
    """完整性检查和清理必须同时约束 tenant、generation、version 与 profile。"""

    observed_requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        """REST返回同collection多generation，删除仍使用完整where。"""

        observed_requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "objects": [
                        {
                            "properties": {
                                "tenant_id": "tenant-a",
                                "version_id": "version-1",
                                "generation_id": "generation-1",
                                "sparse_profile": _profile().key,
                                "chunk_id": "child-1",
                            }
                        },
                        {
                            "properties": {
                                "tenant_id": "tenant-a",
                                "version_id": "version-1",
                                "generation_id": "generation-1",
                                "sparse_profile": _profile().key,
                                "chunk_id": "child-2",
                            }
                        },
                        {
                            "properties": {
                                "tenant_id": "tenant-a",
                                "version_id": "other-version",
                                "generation_id": "other-generation",
                                "sparse_profile": _profile().key,
                                "chunk_id": "other-child",
                            }
                        },
                    ]
                },
                request=request,
            )
        return httpx.Response(
            200,
            json={"results": {"matches": 2, "successful": 2, "failed": 0}},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = WeaviateBM25FTransport(client, "http://weaviate.test")
        snapshot = await transport.inspect_generation(
            "SparseTenant_demo",
            tenant_id="tenant-a",
            generation_id="generation-1",
            version_id="version-1",
            sparse_profile_key=_profile().key,
        )
        deleted = await transport.delete_generation(
            "SparseTenant_demo",
            tenant_id="tenant-a",
            generation_id="generation-1",
            version_id="version-1",
            sparse_profile_key=_profile().key,
        )

    delete_where = json.loads(observed_requests[1].content)["match"]["where"]
    expected_values = ["tenant-a", "generation-1", "version-1", _profile().key]
    assert "where" not in observed_requests[0].url.params
    assert [item["valueText"] for item in delete_where["operands"]] == expected_values
    assert snapshot.item_count == 2
    assert snapshot.chunk_ids == frozenset({"child-1", "child-2"})
    assert snapshot.sparse_profile_key == _profile().key
    assert deleted == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt_field", ["tenant_id", "sparse_profile"])
async def test_sparse_inspection_rejects_same_generation_scope_corruption(corrupt_field) -> None:
    """即使generation/version匹配，tenant或profile不一致也必须安全失败。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        """构造具有相同generation、但授权或配置scope被篡改的响应。"""
        properties = {
            "tenant_id": "tenant-a", "version_id": "version-1",
            "generation_id": "generation-1", "sparse_profile": _profile().key,
            "chunk_id": "child-1",
        }
        properties[corrupt_field] = "other"
        return httpx.Response(200, json={"objects": [{"properties": properties}]}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = WeaviateBM25FTransport(client, "http://weaviate.test")
        with pytest.raises(WeaviateTransportError) as captured:
            await transport.inspect_generation(
                "SparseTenant_demo", tenant_id="tenant-a", generation_id="generation-1",
                version_id="version-1", sparse_profile_key=_profile().key,
            )
        assert captured.value.retryable is False
