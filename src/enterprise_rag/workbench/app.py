"""调用 F2-G API 的最小 Gradio 工作台。

工作台不直接调用模型，也不保存用户输入；所有生成和引用核验仍由 FastAPI 服务完成。
"""

from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx

_DEFAULT_PRODUCT_API_BASE_URL = "http://127.0.0.1:8000"
_WORKBENCH_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "collection",
        "collection_name",
        "dsn",
        "endpoint",
        "object_key",
        "password",
        "prompt",
        "secret",
        "storage_key",
        "token",
        "url",
    }
)


def build_workbench(api_client: httpx.AsyncClient | None = None):
    """构建 question/evidence 输入和结果输出控件。"""

    try:
        import gradio as gr
    except ImportError as exc:
        raise RuntimeError("未安装可选依赖 gradio，请安装项目的 workbench 依赖") from exc

    client = api_client

    async def submit(question: str, evidence_json: str) -> str:
        """只调用同一 JSON API，避免工作台与服务出现第二套生成逻辑。"""

        try:
            evidence = json.loads(evidence_json)
            request = {"question": question, "evidence": evidence}
            if client is None:
                async with httpx.AsyncClient(base_url="http://127.0.0.1:8000") as local_client:
                    response = await local_client.post("/api/v1/query", json=request)
            else:
                response = await client.post("/api/v1/query", json=request)
            return json.dumps(response.json(), ensure_ascii=False, indent=2)
        except (json.JSONDecodeError, TypeError, httpx.HTTPError) as exc:
            return json.dumps({"error": "请求失败", "detail": type(exc).__name__}, ensure_ascii=False)

    with gr.Blocks(title="EnterpriseKnowledgeRAG F2-G") as blocks:
        gr.Markdown("## F2-G 原文摘录联调")
        question = gr.Textbox(label="问题")
        evidence = gr.Textbox(
            label="Evidence JSON",
            value='[{"evidence_id":"doc-1","text":"请填写原文"}]',
            lines=8,
        )
        with gr.Row():
            submit_button = gr.Button("提交", variant="primary")
            clear_button = gr.ClearButton([question, evidence])
        result = gr.Code(label="结果", language="json")
        submit_button.click(submit, inputs=[question, evidence], outputs=result)
        clear_button.click(lambda: "", outputs=result)
    return blocks


def mount_workbench(app: Any) -> None:
    """将 Gradio 工作台挂载到 FastAPI；缺少可选依赖时显式报错。"""

    try:
        import gradio as gr
    except ImportError as exc:
        raise RuntimeError("未安装可选依赖 gradio，请安装项目的 workbench 依赖") from exc
    blocks = build_workbench()
    gr.mount_gradio_app(app, blocks, path="/workbench")


def _sanitize_display_value(value: Any) -> Any:
    """递归移除工作台不应展示的内部配置、对象 key 和凭据字段。"""

    if isinstance(value, dict):
        return {
            key: _sanitize_display_value(item)
            for key, item in value.items()
            if key.lower() not in _WORKBENCH_SENSITIVE_KEYS
        }
    if isinstance(value, list):
        return [_sanitize_display_value(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_display_value(item) for item in value]
    return value


def summarize_product_response(status_code: int, payload: Any) -> dict[str, Any]:
    """将产品 API 响应压缩为工作台可展示的安全摘要。"""

    if 200 <= status_code < 300:
        data = _sanitize_display_value(payload) if isinstance(payload, dict) else {}
        status = "passed"
        if data.get("task_id"):
            # 受理只证明任务已保存；必须等真实阶段终态才显示完成。
            status = {
                "queued": "processing", "running": "processing",
                "retry_wait": "processing", "succeeded": "passed", "dead_letter": "failed",
            }.get(data.get("status"), "failed")
            if status_code == 202 and status != "failed":
                status = "accepted"
        answer_status = data.get("answer_status")
        if answer_status == "incomplete":
            status = "incomplete"
        elif answer_status in {"abstained", "not_found"}:
            status = "abstained"
        elif answer_status is not None and (
            answer_status != "answered" or data.get("citation_validation") != "passed"
        ):
            status = "failed"
        summary = {"status_code": status_code, "status": status, "data": data}
        if status == "incomplete":
            summary["error_kind"] = "output_limit"
        return summary

    # 认证失败采用固定提示，不回显签名、token内容或上游异常正文。
    if status_code == 401:
        return {
            "status_code": 401,
            "status": "failed",
            "error_kind": "authentication_required",
            "message": "开发身份 Token 无效或已过期，请填写新签发的 Token，而不是模型 API Key。",
        }

    error_kind = None
    if isinstance(payload, dict):
        # FastAPI HTTPException使用detail，业务JSON错误使用error，均只提取安全类别。
        error = payload.get("error") or payload.get("detail")
        if isinstance(error, dict) and isinstance(error.get("kind"), str):
            candidate = error["kind"]
            if candidate and len(candidate) <= 64 and all(
                character.islower() or character.isdigit() or character == "_"
                for character in candidate
            ):
                error_kind = candidate
    summary: dict[str, Any] = {
        "status_code": status_code,
        "status": "blocked" if status_code == 404 else "failed",
    }
    if error_kind is not None:
        summary["error_kind"] = error_kind
    return summary


def _response_payload(response: httpx.Response | Any) -> Any:
    """读取 JSON 响应；解析失败时返回空对象而不回显响应正文。"""

    try:
        return response.json()
    except (TypeError, ValueError):
        return {}


def _authorization_headers(token: str) -> dict[str, str]:
    """规范输入后构造Bearer header；只去掉首尾空白和可选前缀，不改内部签名。"""

    value = token.strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    return {"Authorization": f"Bearer {value}"}


def _resolve_upload_path(file_path: Any) -> Path | None:
    """兼容 Gradio filepath、FileData 字典和 Path 输入，返回本地临时文件路径。"""

    if isinstance(file_path, dict):
        file_path = file_path.get("path") or file_path.get("name")
    else:
        file_path = getattr(file_path, "path", file_path)
    if not isinstance(file_path, (str, Path)):
        return None
    path = Path(file_path)
    return path if path.is_file() else None


async def upload_product_document(
    client: httpx.AsyncClient,
    *,
    token: str,
    file_path: Any,
    source_id: str,
    title: str,
    idempotency_key: str,
    anonymous: bool = False,
    async_ingestion: bool = False,
) -> dict[str, Any]:
    """通过产品上传 API 提交文档，不在工作台复制摄取或发布逻辑。"""

    path = _resolve_upload_path(file_path)
    if not anonymous and not token.strip():
        return {"status_code": 400, "status": "failed", "error_kind": "missing_token"}
    if path is None:
        return {"status_code": 400, "status": "failed", "error_kind": "file_required"}

    # 工作台只提交产品 API 明确允许的 multipart 字段；tenant、actor、version
    # 和索引配置全部由服务端从 Bearer token 与固定运行配置中解析。
    # 逐块hash并传文件句柄，避免UI为大文件创建整份bytes副本。
    with path.open("rb") as content:
        digest = hashlib.file_digest(content, "sha256").hexdigest()
        content.seek(0)
        response = await client.post(
            "/api/v1/ingestion/tasks" if async_ingestion else "/api/v1/documents",
            headers={} if anonymous else _authorization_headers(token),
            data={
                "source_id": source_id.strip() or f"upload:{path.name}",
                "title": title.strip() or path.stem,
                # 相同文件和来源重试复用幂等键；修改内容后自然生成新键。
                "idempotency_key": idempotency_key.strip() or hashlib.sha256(
                    f"{source_id.strip() or f'upload:{path.name}'}:{title.strip() or path.stem}:{digest}".encode()
                ).hexdigest(),
            },
            files={"file": (path.name, content, {
                ".txt": "text/plain",
                ".md": "text/markdown", ".markdown": "text/markdown",
                ".html": "text/html", ".htm": "text/html", ".pdf": "application/pdf",
            }.get(path.suffix.lower(), "application/octet-stream"))},
            # 同步摄取大文档可能耗时很久；仅文件上传不使用问答的300秒预算。
            timeout=None,
        )
    return summarize_product_response(response.status_code, _response_payload(response))


def _resolve_upload_paths(file_path: Any) -> list[Path]:
    """把Gradio单文件或多文件值统一为存在的本地路径列表。"""
    if isinstance(file_path, (list, tuple)):
        values = file_path
    else:
        values = [file_path]
    resolved: list[Path] = []
    for value in values:
        path = _resolve_upload_path(value)
        if path is not None:
            resolved.append(path)
    return resolved


async def upload_product_documents(
    client: httpx.AsyncClient,
    *,
    token: str,
    file_paths: Any,
    source_id: str = "",
    title: str = "",
    idempotency_key: str = "",
    anonymous: bool = False,
    async_ingestion: bool = False,
) -> dict[str, Any]:
    """逐个上传多份文档并汇总结果；单个失败不阻断其他文档。"""
    paths = _resolve_upload_paths(file_paths)
    if not paths:
        return {"status_code": 400, "status": "failed", "error_kind": "file_required"}
    results = []
    for path in paths:
        # 多文件共用输入只作前缀，每个文件仍必须具有独立source和幂等身份。
        per_source = f"{source_id.strip()}:{path.name}" if source_id.strip() and len(paths) > 1 else source_id
        per_key = hashlib.sha256(f"{idempotency_key.strip()}:{path.name}".encode()).hexdigest() if (
            idempotency_key.strip() and len(paths) > 1
        ) else idempotency_key
        try:
            result = await upload_product_document(
                client, token=token, file_path=path, source_id=per_source,
                title=title, idempotency_key=per_key, anonymous=anonymous,
                async_ingestion=async_ingestion,
            )
        except (httpx.HTTPError, OSError):
            # 只保存安全类别，不将网络地址/文件路径/异常正文放入批量回包。
            result = {"status": "failed", "error_kind": "upload_unavailable"}
        results.append({"filename": path.name, "result": result})
    if async_ingestion:
        accepted = all(
            item["result"].get("status") in {"accepted", "processing", "passed"} for item in results
        )
        return {
            "status_code": 202 if accepted else 207,
            "status": "accepted" if accepted else "partial", "count": len(results),
            "results": results,
        }
    passed = all(item["result"].get("status") == "passed" for item in results)
    return {
        "status_code": 200 if passed else 207,
        "status": "passed" if passed else "partial",
        "count": len(results),
        "results": results,
    }


async def get_product_document_status(
    client: httpx.AsyncClient,
    *,
    token: str,
    document_id: str,
    anonymous: bool = False,
) -> dict[str, Any]:
    """通过产品状态 API 查询文档、版本、任务和 generation 状态摘要。"""

    if (not anonymous and not token.strip()) or not document_id.strip():
        return {"status_code": 400, "status": "failed", "error_kind": "invalid_request"}
    identity = document_id.strip()
    # 服务端任务ID固定task_前缀；旧Document ID状态路径保持兼容。
    path = (
        f"/api/v1/ingestion/tasks/{identity}" if identity.startswith("task_")
        else f"/api/v1/documents/{identity}/status"
    )
    response = await client.get(
        path,
        headers={} if anonymous else _authorization_headers(token),
    )
    return summarize_product_response(response.status_code, _response_payload(response))


async def query_product(
    client: httpx.AsyncClient,
    *,
    token: str,
    question: str,
    top_k: int = 5,
    candidate_k: int = 20,
    max_tokens: int = 8192,
    timeout_ms: int = 300_000,
    anonymous: bool = False,
) -> dict[str, Any]:
    """通过产品问答 API 查询服务端授权证据并展示回答、引用和检索摘要。"""

    if (not anonymous and not token.strip()) or not question.strip():
        return {"status_code": 400, "status": "failed", "error_kind": "invalid_request"}
    response = await client.post(
        "/api/v1/rag/query",
        headers={} if anonymous else _authorization_headers(token),
        json={
            "question": question,
            "top_k": top_k,
            "candidate_k": candidate_k,
            "max_tokens": max_tokens,
            "timeout_ms": timeout_ms,
        },
    )
    return summarize_product_response(response.status_code, _response_payload(response))


@asynccontextmanager
async def _product_client_scope(
    api_client: httpx.AsyncClient | None,
    api_base_url: str,
):
    """复用注入 client，或为当前 Gradio 请求创建并关闭短生命周期 client。"""

    if api_client is not None:
        yield api_client
        return
    async with httpx.AsyncClient(
        base_url=api_base_url.rstrip("/"),
        timeout=httpx.Timeout(310.0),
    ) as client:
        yield client


def build_product_workbench(
    api_client: httpx.AsyncClient | None = None,
    *,
    api_base_url: str = _DEFAULT_PRODUCT_API_BASE_URL,
    anonymous: bool = False,
    async_ingestion: bool = False,
):
    """构建方案 C P0 工作台：上传、状态查询和带引用产品问答。"""

    try:
        import gradio as gr
    except ImportError as exc:
        raise RuntimeError("未安装可选依赖 gradio，请安装项目的 workbench 依赖") from exc

    async def upload_action(
        token: str,
        file_paths: Any,
        source_id: str,
        title: str,
        idempotency_key: str,
    ) -> tuple[str, str]:
        """处理上传按钮事件，并把 document_id 回填给状态查询控件。"""

        async with _product_client_scope(api_client, api_base_url) as client:
            summary = await upload_product_documents(
                client,
                token=token,
                file_paths=file_paths,
                source_id=source_id,
                title=title,
                idempotency_key=idempotency_key,
                anonymous=anonymous,
                async_ingestion=async_ingestion,
            )
        document_ids = [
            item["result"].get("data", {}).get("document_id")
            or item["result"].get("data", {}).get("task_id", "")
            for item in summary.get("results", [])
        ]
        # 单文件保持已有上传响应契约；多文件才展示逐文件汇总。
        if len(summary.get("results", [])) == 1:
            summary = summary["results"][0]["result"]
        return json.dumps(summary, ensure_ascii=False, indent=2), "\n".join(filter(None, document_ids))

    async def status_action(token: str, document_id: str) -> str:
        """处理单个或多行Document ID，逐个展示服务端安全状态摘要。"""

        async with _product_client_scope(api_client, api_base_url) as client:
            identities = [value.strip() for value in document_id.splitlines() if value.strip()]
            summaries = [
                await get_product_document_status(
                    client, token=token, document_id=value, anonymous=anonymous,
                )
                for value in identities
            ]
            summary = summaries[0] if len(summaries) == 1 else {"results": summaries}
        return json.dumps(summary, ensure_ascii=False, indent=2)

    async def query_action(token: str, question: str, top_k: int, candidate_k: int, max_tokens: int):
        """先显示处理中状态，再显示经引用验证的回答及安全trace。"""

        yield "正在检索并生成回答...", ""
        async with _product_client_scope(api_client, api_base_url) as client:
            summary = await query_product(
                client,
                token=token,
                question=question,
                top_k=int(top_k),
                candidate_k=int(candidate_k),
                max_tokens=int(max_tokens),
                anonymous=anonymous,
            )
        data = summary.get("data", {})
        # 仅业务回答完成且引用通过时显示答案，不展示截断或未核验的模型正文。
        answer = data.get("message", "") if summary["status"] == "passed" else (
            data.get("message") or f"查询未完成：{summary['status']}"
        )
        yield answer, json.dumps(summary, ensure_ascii=False, indent=2)

    with gr.Blocks(title="EnterpriseKnowledgeRAG 本地 P0") as blocks:
        gr.Markdown("## 本地 P0 产品工作台")
        token = gr.Textbox(
            label="开发 Token", type="password", placeholder="项目 API 身份凭证（非模型密钥）",
            visible=not anonymous,
        )

        with gr.Tab("上传与状态"):
            file_input = gr.File(
                label="文档",
                file_count="multiple",
                type="filepath",
                file_types=[".txt", ".md", ".markdown", ".html", ".htm", ".pdf"],
            )
            source_id = gr.Textbox(label="Source ID")
            title = gr.Textbox(label="标题")
            idempotency_key = gr.Textbox(label="幂等键")
            upload_button = gr.Button(
                "提交入库" if async_ingestion else "上传并发布", variant="primary",
            )
            upload_result = gr.Code(label="上传结果", language="json")
            document_id = gr.Textbox(
                label="Task ID / Document ID" if async_ingestion else "Document ID", lines=3,
            )
            status_button = gr.Button("查询状态")
            status_result = gr.Code(label="文档状态", language="json")

            upload_button.click(
                upload_action,
                inputs=[token, file_input, source_id, title, idempotency_key],
                outputs=[upload_result, document_id],
            )
            status_button.click(
                status_action,
                inputs=[token, document_id],
                outputs=status_result,
            )

        with gr.Tab("检索问答"):
            question = gr.Textbox(label="问题", lines=3)
            top_k = gr.Slider(label="Top K", minimum=1, maximum=20, value=5, step=1)
            candidate_k = gr.Slider(
                label="Candidate K",
                minimum=1,
                maximum=100,
                value=20,
                step=1,
            )
            max_tokens = gr.Slider(label="Max tokens", minimum=1, maximum=8192, value=8192, step=1)
            query_button = gr.Button("查询并回答", variant="primary")
            answer = gr.Textbox(label="回答", lines=10, interactive=False)
            query_result = gr.Code(label="回答、引用与检索摘要", language="json")
            query_button.click(
                query_action,
                inputs=[token, question, top_k, candidate_k, max_tokens],
                outputs=[answer, query_result],
            )
    return blocks


def mount_product_workbench(
    app: Any, *, path: str = "/product-workbench",
    api_base_url: str = _DEFAULT_PRODUCT_API_BASE_URL,
    anonymous: bool = False,
    async_ingestion: bool = False,
) -> None:
    """将方案 C P0 工作台挂载到 FastAPI，缺少 Gradio 时显式失败。"""

    try:
        import gradio as gr
    except ImportError as exc:
        raise RuntimeError("未安装可选依赖 gradio，请安装项目的 workbench 依赖") from exc
    blocks = build_product_workbench(
        api_base_url=api_base_url, anonymous=anonymous, async_ingestion=async_ingestion,
    )
    # Gradio6的标准upload_fn固定max_files=1000；项目单独注册兼容上传路径，
    # 复用它的磁盘multipart parser和文件缓存信任集合，不修改第三方全局对象。
    from .uploads import mount_unlimited_upload

    mount_unlimited_upload(app, blocks, path=path)
    gr.mount_gradio_app(
        app, blocks,
        path=path, max_file_size=None,
    )


__all__ = [
    "build_product_workbench",
    "build_workbench",
    "get_product_document_status",
    "mount_product_workbench",
    "mount_workbench",
    "query_product",
    "summarize_product_response",
    "upload_product_document",
    "upload_product_documents",
]
