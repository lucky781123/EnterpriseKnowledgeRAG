"""旧UI兼容入口的API地址与产品工作台复用回归。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "gradio_enhanced", Path(__file__).resolve().parents[1] / "scripts/gradio_enhanced.py",
)
legacy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(legacy)


def test_legacy_ui_uses_managed_product_address(tmp_path, monkeypatch) -> None:
    """无显式配置时使用一键启动的实际端口，而不是写死8000。"""

    monkeypatch.delenv("P0_API_BASE_URL", raising=False)
    state = tmp_path / "service.json"
    state.write_text('{"api":"http://127.0.0.1:8001"}', encoding="utf-8")
    assert legacy.resolve_api_base_url(state) == "http://127.0.0.1:8001"


def test_legacy_ui_requires_explicit_or_managed_product_address(tmp_path, monkeypatch) -> None:
    """没有运行状态时必须明确失败，不能将Token误送到猜测的服务。"""

    monkeypatch.delenv("P0_API_BASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="start.sh"):
        legacy.resolve_api_base_url(tmp_path / "missing.json")


@pytest.mark.parametrize("url", [
    "http://example.com:8001", "http://secret@127.0.0.1:8001",
    "http://127.0.0.1:8001/?secret=value",
])
def test_legacy_ui_rejects_remote_or_credential_bearing_addresses(url, monkeypatch) -> None:
    """不允许本地开发身份被错误发送到远程或带凭据的地址。"""

    monkeypatch.setenv("P0_API_BASE_URL", url)
    with pytest.raises(RuntimeError, match="本地"):
        legacy.resolve_api_base_url()


def test_legacy_ui_reuses_authenticated_product_workbench(monkeypatch) -> None:
    """复用现有Bearer上传和rag/query契约，不再调用caller-supplied问答。"""

    received = []

    def build(**kwargs):
        """记录工厂收到的API地址，避免启动真实Gradio服务。"""
        received.append(kwargs)
        return "product-ui"

    monkeypatch.setattr(legacy, "build_product_workbench", build)
    assert legacy.create_interface(api_base_url="http://127.0.0.1:8001") == "product-ui"
    assert received == [{"api_base_url": "http://127.0.0.1:8001"}]


def test_legacy_bookmark_redirects_without_query_request_parameter() -> None:
    """旧书签必须直接跳转，不误将Request注解当作必填query字段。"""
    from fastapi.testclient import TestClient

    app = legacy.create_redirect_app("http://127.0.0.1:8001")
    with TestClient(app, client=("127.0.0.1", 1234)) as client:
        response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "http://127.0.0.1:8001/product-workbench/"
