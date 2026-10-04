"""通过实际挂载Gradio上传路由验证请求体预算，而不只测产品API路径。"""

import json

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("gradio")

from enterprise_rag.api.app import create_app
from enterprise_rag.workbench.app import mount_product_workbench


def test_gradio_upload_above_128kib_reaches_file_handler() -> None:
    """Gradio临时文件上传必须获得文件预算，而非普通JSON的128KiB预算。"""

    app = create_app()
    mount_product_workbench(app)
    with TestClient(app) as client:
        response = client.post(
            "/product-workbench/gradio_api/upload",
            files={"files": ("budget.pdf", b"%PDF-" + b"x" * (256 * 1024), "application/pdf")},
        )
    assert response.status_code == 200
    assert len(response.json()) == 1


def test_file_budget_does_not_relax_ordinary_query_budget() -> None:
    """放宽文件上传及批量元数据路径不得放开普通API问答JSON预算。"""
    with TestClient(create_app()) as client:
        response = client.post("/api/v1/rag/query", content=b"x" * (129 * 1024))
    assert response.status_code == 413
    assert response.json()["error"]["message"] == "请求体超过 128 KiB"


def test_gradio_upload_accepts_over_10mib_and_multiple_files() -> None:
    """实际临时上传路由必须接收超过旧上限的文件和同批多文件。"""
    app = create_app()
    mount_product_workbench(app)
    with TestClient(app) as client:
        response = client.post(
            "/product-workbench/gradio_api/upload",
            files=[
                ("files", ("large.pdf", b"%PDF-" + b"x" * (11 * 1024 * 1024), "application/pdf")),
                ("files", ("other.txt", b"public text", "text/plain")),
            ],
        )
    assert response.status_code == 200
    assert len(response.json()) == 2


def test_gradio_upload_accepts_more_than_framework_default_file_count() -> None:
    """1001份小文件证明不是仅去掉单文件控件却保留1000数量上限。"""
    app = create_app()
    mount_product_workbench(app)
    with TestClient(app) as client:
        response = client.post(
            "/product-workbench/gradio_api/upload",
            files=[("files", (f"sample-{index}.txt", b"public", "text/plain")) for index in range(1001)],
        )
    assert response.status_code == 200
    assert len(response.json()) == 1001


def test_gradio_queue_join_accepts_large_batch_metadata() -> None:
    """大量已上传文件的路径元数据不能被普通128 KiB JSON预算截断。"""
    app = create_app()
    mount_product_workbench(app)
    with TestClient(app) as client:
        payload = {
            "data": [
                {
                    "path": f"/tmp/gradio-upload-{index:04d}.txt",
                    "orig_name": f"sample-{index:04d}.txt",
                }
                for index in range(2000)
            ],
            "fn_index": 0,
            "session_hash": "batch-test",
        }
        assert len(json.dumps(payload).encode()) > 128 * 1024
        response = client.post("/product-workbench/gradio_api/queue/join", json=payload)
    # 本测试只验证请求体预算已从普通JSON路径分离；Gradio随后会因不存在
    # 临时文件返回业务错误，但不能在middleware阶段返回413。
    assert response.status_code != 413
