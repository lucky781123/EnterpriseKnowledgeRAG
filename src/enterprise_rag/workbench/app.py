"""调用 F2-G API 的最小 Gradio 工作台。

工作台不直接调用模型，也不保存用户输入；所有生成和引用核验仍由 FastAPI 服务完成。
"""

from __future__ import annotations

import json
from typing import Any

import httpx


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
