"""F2-I 与本地 P0 工作台的依赖和调用边界测试。"""

from __future__ import annotations

import pytest

from enterprise_rag.workbench.app import (
    build_product_workbench,
    build_workbench,
    get_product_document_status,
    query_product,
    summarize_product_response,
    upload_product_document,
    upload_product_documents,
)


class _Response:
    """模拟 HTTPX 响应，避免工作台契约测试依赖外部 FastAPI。"""

    def __init__(self, status_code: int, payload: object) -> None:
        """保存状态码和 JSON 载荷，提供工作台所需的最小响应接口。"""

        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        """返回预置 JSON 载荷。"""

        return self._payload


class _Client:
    """记录产品工作台请求，验证请求字段不会混入身份或内部配置。"""

    def __init__(self) -> None:
        """初始化请求记录和固定响应。"""

        self.calls: list[dict[str, object]] = []

    async def post(self, path: str, **kwargs: object) -> _Response:
        """记录 POST 请求并返回安全的产品响应。"""

        self.calls.append({"method": "POST", "path": path, **kwargs})
        if path == "/api/v1/documents":
            return _Response(
                201,
                {
                    "document_id": "doc-1",
                    "version_id": "version-1",
                    "generation_id": "generation-1",
                    "published": True,
                },
            )
        return _Response(
            200,
            {
                "answer": "回答",
                "citations": [{"quote": "原文", "source": "guide.md"}],
                "retrieval_trace": {"dense_hits": 1, "reranker_used": True},
            },
        )

    async def get(self, path: str, **kwargs: object) -> _Response:
        """记录 GET 请求并返回固定状态摘要。"""

        self.calls.append({"method": "GET", "path": path, **kwargs})
        return _Response(200, {"document_id": "doc-1", "document_status": "ready"})


def test_workbench_reports_optional_dependency_boundary() -> None:
    """未安装 Gradio 时应给出可操作的明确错误，而不是静默失败。"""

    try:
        import gradio  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match="gradio"):
            build_workbench()
        with pytest.raises(RuntimeError, match="gradio"):
            build_product_workbench()


def test_product_workbench_summarizes_only_safe_response_fields() -> None:
    """产品工作台应保留回答和引用，同时删除内部 key 与凭据字段。"""

    summary = summarize_product_response(
        200,
        {
            "answer": "回答",
            "citations": [{"quote": "原文"}],
            "collection_name": "tenant_internal_collection",
            "object_key": "raw/secret-key",
            "token": "do-not-display",
        },
    )

    assert summary == {
        "status_code": 200,
        "status": "passed",
        "data": {"answer": "回答", "citations": [{"quote": "原文"}]},
    }


@pytest.mark.asyncio
async def test_product_workbench_upload_and_query_use_product_api_contract(tmp_path) -> None:
    """上传和问答只能提交产品字段，不能由工作台伪造 tenant/evidence/endpoint。"""

    client = _Client()
    document = tmp_path / "guide.md"
    document.write_text("# Guide\n", encoding="utf-8")

    upload = await upload_product_document(
        client,
        token="secret-token",
        file_path=document,
        source_id="upload:guide",
        title="指南",
        idempotency_key="idem-1",
    )
    query = await query_product(
        client,
        token="secret-token",
        question="如何部署？",
        top_k=3,
        candidate_k=10,
        max_tokens=128,
        timeout_ms=60_000,
    )

    assert upload["status"] == "passed"
    assert query["data"]["answer"] == "回答"
    upload_call = client.calls[0]
    query_call = client.calls[1]
    assert upload_call["path"] == "/api/v1/documents"
    assert upload_call["data"] == {
        "source_id": "upload:guide",
        "title": "指南",
        "idempotency_key": "idem-1",
    }
    assert upload_call["files"]["file"][2] == "text/markdown"
    assert upload_call["headers"] == {"Authorization": "Bearer secret-token"}
    assert query_call["path"] == "/api/v1/rag/query"
    assert query_call["headers"] == {"Authorization": "Bearer secret-token"}
    assert query_call["json"] == {
        "question": "如何部署？",
        "top_k": 3,
        "candidate_k": 10,
        "max_tokens": 128,
        "timeout_ms": 60_000,
    }
    assert "tenant_id" not in query_call["json"]
    assert "evidence" not in query_call["json"]


def test_product_workbench_error_summary_does_not_echo_error_body() -> None:
    """错误响应只展示固定类别，避免把服务端详情回显到工作台。"""

    summary = summarize_product_response(
        503,
        {
            "error": {
                "kind": "retrieval_unavailable",
                "message": "secret endpoint and collection details",
            }
        },
    )

    assert summary == {
        "status_code": 503,
        "status": "failed",
        "error_kind": "retrieval_unavailable",
    }


def test_authentication_error_is_actionable_without_echoing_secrets() -> None:
    """401必须明确指向开发身份，不能回显上游输入或要求模型密钥。"""

    summary = summarize_product_response(
        401, {"detail": {"kind": "authentication_required", "message": "private-token-secret"}},
    )
    assert summary["error_kind"] == "authentication_required"
    assert "开发身份 Token" in summary["message"]
    assert "private-token-secret" not in str(summary)


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["  secret-token\n", "Bearer secret-token", "bearer  secret-token "])
async def test_token_input_trims_whitespace_and_optional_bearer_prefix(token) -> None:
    """粘贴完整Authorization或末尾换行时，不重复Bearer也不把换行发送给HTTPX。"""

    client = _Client()
    await query_product(client, token=token, question="部署步骤？")
    assert client.calls[0]["headers"] == {"Authorization": "Bearer secret-token"}


@pytest.mark.asyncio
async def test_anonymous_upload_accepts_txt_and_fills_metadata(tmp_path) -> None:
    """本机匿名UI允许不填Token和元数据，自动使用文件名/内容hash生成业务字段。"""

    client = _Client()
    document = tmp_path / "plain.txt"
    document.write_text("公开TXT正文", encoding="utf-8")
    result = await upload_product_document(
        client, token="", file_path=document, source_id="", title="", idempotency_key="",
        anonymous=True,
    )
    assert result["status"] == "passed"
    call = client.calls[0]
    assert call["headers"] == {}
    assert call["files"]["file"][2] == "text/plain"
    assert call["data"]["title"] == "plain"
    assert call["data"]["source_id"].startswith("upload:")
    assert call["data"]["idempotency_key"]


@pytest.mark.asyncio
async def test_anonymous_status_and_query_send_no_authorization() -> None:
    """匿名状态和问答必须显式省略身份header，不能依赖旧Token文件。"""
    client = _Client()
    status = await get_product_document_status(
        client, token="", document_id="doc-1", anonymous=True,
    )
    query = await query_product(client, token="", question="公开问题", anonymous=True)
    assert status["status"] == query["status"] == "passed"
    assert all(call["headers"] == {} for call in client.calls)


@pytest.mark.asyncio
async def test_batch_metadata_does_not_merge_documents(tmp_path) -> None:
    """批量共用元数据前缀不能使两个文件成为同一document或冲突幂等任务。"""
    client = _Client()
    paths = [tmp_path / "first.txt", tmp_path / "second.txt"]
    for path in paths:
        path.write_text(path.stem, encoding="utf-8")
    summary = await upload_product_documents(
        client, token="", file_paths=paths, source_id="batch", title="公开样本",
        idempotency_key="batch-request", anonymous=True,
    )
    assert summary["status"] == "passed"
    assert summary["count"] == 2
    assert len({call["data"]["source_id"] for call in client.calls}) == 2
    assert len({call["data"]["idempotency_key"] for call in client.calls}) == 2


@pytest.mark.asyncio
async def test_batch_continues_after_transport_failure(tmp_path) -> None:
    """单个上传网络失败不丢失其状态，也不阻断后续文件。"""
    import httpx

    class FailsFirst(_Client):
        """第一份上传失败，第二份走已有成功响应。"""

        async def post(self, path: str, **kwargs: object) -> _Response:
            """使用显式连接异常模拟服务短暂不可达。"""
            if kwargs["files"]["file"][0] == "first.txt":
                raise httpx.ConnectError("不应显示的内部地址")
            return await super().post(path, **kwargs)

    paths = [tmp_path / "first.txt", tmp_path / "second.txt"]
    for path in paths:
        path.write_text("公开样本", encoding="utf-8")
    summary = await upload_product_documents(
        FailsFirst(), token="", file_paths=paths, anonymous=True,
    )
    assert summary["status"] == "partial"
    assert summary["results"][0]["result"]["status"] == "failed"
    assert summary["results"][1]["result"]["status"] == "passed"
    assert "内部地址" not in str(summary)


def test_product_ui_accepts_multiple_files() -> None:
    """真实Gradio配置必须为multiple，而不仅是批量辅助函数通过。"""
    pytest.importorskip("gradio")
    config = build_product_workbench(anonymous=True).get_config_file()
    component = next(item for item in config["components"] if item["props"].get("label") == "文档")
    assert component["props"]["file_count"] == "multiple"


def test_http200_truncated_answer_is_not_passed() -> None:
    """HTTP成功不等于回答完成，截断必须明确标记且不透出未核验正文。"""
    summary = summarize_product_response(200, {
        "answer_status": "incomplete", "citation_validation": "not_checked",
        "message": "模型输出达到长度上限，回答未完整生成", "citations": [],
        "provider_result": {"finish_reason": "length", "content": None},
    })
    assert summary["status"] == "incomplete"
    assert summary["error_kind"] == "output_limit"


@pytest.mark.parametrize(
    ("answer_status", "validation", "expected"),
    [("answered", "failed", "failed"), ("invalid_citations", "failed", "failed"),
     ("abstained", "not_applicable", "abstained"), ("answered", "passed", "passed")],
)
def test_answer_summary_requires_business_validation(answer_status, validation, expected) -> None:
    """回答完成和引用通过必须同时成立，合法拒答也不能冒充已回答。"""
    summary = summarize_product_response(200, {
        "answer_status": answer_status, "citation_validation": validation,
        "message": "诊断结果", "citations": [],
    })
    assert summary["status"] == expected


@pytest.mark.asyncio
async def test_product_query_default_budget_covers_multi_clause_answer() -> None:
    """多条文问答默认需容纳reasoning/答案/引用，但显式小预算仍由调用方控制。"""
    from enterprise_rag.api.product_schemas import ProductRagQueryRequest

    client = _Client()
    await query_product(client, token="", question="相关条文", anonymous=True)
    request = client.calls[0]["json"]
    assert request["max_tokens"] == ProductRagQueryRequest(question="相关条文").max_tokens == 8192
    assert request["timeout_ms"] == ProductRagQueryRequest(question="相关条文").timeout_ms == 300_000


def test_product_ui_budget_default_and_pending_output() -> None:
    """验证UI初始预算及多输出事件，防止再次用显式测试参数掩盖用户默认值。"""
    pytest.importorskip("gradio")
    blocks = build_product_workbench(anonymous=True)
    config = blocks.get_config_file()
    components = config["components"]
    slider = next(item for item in components if item["props"].get("label") == "Max tokens")
    assert slider["props"]["value"] == 8192
    assert slider["props"]["maximum"] == 8192
    assert any(item["props"].get("label") == "回答" for item in components)
    query_event = next(item for item in config["dependencies"] if item.get("api_name") == "query_action")
    assert query_event["types"]["generator"] is True
    assert len(query_event["outputs"]) == 2


@pytest.mark.parametrize(
    ("http_status", "task_status", "expected"),
    [(202, "queued", "accepted"), (200, "running", "processing"),
     (200, "retry_wait", "processing"), (200, "succeeded", "passed"),
     (200, "dead_letter", "failed")],
)
def test_async_task_is_not_finished_on_accept(http_status, task_status, expected):
    """202 与进行中状态不能显示为入库完成，死信也不能因 HTTP200 标成成功。"""
    result = summarize_product_response(http_status, {
        "task_id": "task_public", "status": task_status, "stage": "parse",
    })
    assert result["status"] == expected


async def test_async_ui_upload_and_task_status_use_new_routes(tmp_path):
    """异步UI不调用旧同步上传，状态查询用任务ID且不自报身份。"""
    class AsyncClient(_Client):
        """只提供异步受理与阶段状态的离线响应。"""

        async def post(self, path, **kwargs):
            """捕获受理字段，响应只含安全任务状态。"""
            self.calls.append({"method": "POST", "path": path, **kwargs})
            return _Response(202, {"task_id": "task_public", "status": "queued", "stage": "parse"})

        async def get(self, path, **kwargs):
            """捕获状态请求，不模拟Worker已完成。"""
            self.calls.append({"method": "GET", "path": path, **kwargs})
            return _Response(200, {"task_id": "task_public", "status": "running", "stage": "embedding"})

    client = AsyncClient()
    path = tmp_path / "public.txt"
    path.write_text("公开测试", encoding="utf-8")
    result = await upload_product_documents(
        client, token="", file_paths=[path], anonymous=True, async_ingestion=True,
    )
    assert result["status"] == "accepted"
    assert client.calls[0]["path"] == "/api/v1/ingestion/tasks"
    status = await get_product_document_status(
        client, token="", document_id="task_public", anonymous=True,
    )
    assert client.calls[1]["path"] == "/api/v1/ingestion/tasks/task_public"
    assert status["status"] == "processing"
    assert all(call["headers"] == {} for call in client.calls)


def test_async_ui_exposes_task_id_and_submit_button():
    """显式异步模式展示Task ID和提交操作；默认同步UI保持原样。"""
    pytest.importorskip("gradio")
    config = build_product_workbench(anonymous=True, async_ingestion=True).get_config_file()
    props = [item["props"] for item in config["components"]]
    assert any(item.get("label") == "Task ID / Document ID" for item in props)
    assert any(item.get("value") == "提交入库" for item in props)
