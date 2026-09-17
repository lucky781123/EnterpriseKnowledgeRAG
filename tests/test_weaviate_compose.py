"""本地 Weaviate Compose 配置的最小契约测试。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _render_compose_config() -> dict[str, object]:
    """使用 Docker Compose 官方解析器读取最终配置，避免只测试 YAML 文本片段。"""

    completed = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_compose_defines_isolated_weaviate_development_service() -> None:
    """Weaviate 必须固定版本、持久化数据并暴露 readiness 健康检查。"""

    config = _render_compose_config()
    services = config["services"]
    weaviate = services["weaviate"]

    # 固定镜像版本，避免未来 latest 漂移破坏当前 REST/GraphQL transport 契约。
    assert weaviate["image"].startswith("semitechnologies/weaviate:1.")
    assert not weaviate["image"].endswith(":latest")
    assert weaviate["ports"][0]["published"] == "8080"
    assert weaviate["healthcheck"]["test"][-1].endswith("/v1/.well-known/ready")
    assert weaviate["environment"]["AUTHENTICATION_ANONYMOUS_ACCESS_ENABLED"] == "true"
    assert weaviate["environment"]["DEFAULT_VECTORIZER_MODULE"] == "none"
    assert any(mount["target"] == "/var/lib/weaviate" for mount in weaviate["volumes"])


def test_api_receives_weaviate_configuration_without_switching_default_read_path() -> None:
    """API 可访问 Compose 内 Weaviate，但默认仍保持禁用并使用 PostgreSQL 读取路径。"""

    config = _render_compose_config()
    api = config["services"]["api"]

    # 这里只注入服务端配置，不改变 create_app() 的默认 repository 选择逻辑。
    assert api["environment"]["WEAVIATE_ENABLED"] == "false"
    assert api["environment"]["WEAVIATE_BASE_URL"] == "http://weaviate:8080"
    assert api["depends_on"]["weaviate"]["condition"] == "service_healthy"


def test_env_example_uses_runtime_weaviate_variable_names() -> None:
    """示例环境变量必须与 Settings 和 smoke runner 实际读取的名称一致。"""

    content = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")

    assert "WEAVIATE_ENABLED=false" in content
    assert "WEAVIATE_BASE_URL=http://127.0.0.1:8080" in content
    assert "WEAVIATE_SCHEMA_VERSION=dense-v1" in content
    assert "WEAVIATE_URL=" not in content
