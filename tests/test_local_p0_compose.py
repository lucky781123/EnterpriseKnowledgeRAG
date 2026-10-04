"""本地 P0 容器入口的静态契约测试。"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_p0_compose_keeps_product_entrypoint_isolated() -> None:
    """P0 覆盖配置必须使用独立服务，且显式启用真实产品依赖。"""

    # 这里读取仓库内的公开 Compose 文本，不读取 .env，也不展开真实配置。
    content = (PROJECT_ROOT / "compose.p0.yaml").read_text(encoding="utf-8")

    assert "p0-api:" in content
    assert "PIP_EXTRAS: reranker" in content
    assert 'WEAVIATE_ENABLED: "true"' in content
    assert 'DEV_AUTH_ENABLED: "true"' in content
    assert 'scripts.local_p0_api:app' in content
    assert "POSTGRES_DSN:?POSTGRES_DSN must be provided for P0 API" in content
    assert "DEV_AUTH_SECRET:?DEV_AUTH_SECRET must be provided for P0 API" in content
    assert "BGE_RERANKER_MODEL:?BGE_RERANKER_MODEL must be provided for P0 API" in content


def test_dockerfile_contains_runtime_scripts_and_optional_reranker_extra() -> None:
    """镜像必须包含本地入口脚本，并能按需安装 BGE 依赖。"""

    # 默认 API 仍不强制安装大体积 reranker 依赖；P0 Compose 通过 build arg 开启。
    content = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY scripts ./scripts" in content
    assert 'ARG PIP_EXTRAS=""' in content
    assert '".[$PIP_EXTRAS]"' in content


def test_env_example_documents_p0_runtime_variables() -> None:
    """公开配置模板必须列出本地 P0 的 reranker 和工作台开关。"""

    # 模板只包含变量名和安全默认值，不应把本地 secret 写入仓库。
    content = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")

    assert "BGE_RERANKER_MODEL=" in content
    assert "BGE_RERANKER_DEVICE=cpu" in content
    assert "P0_WORKBENCH_ENABLED=false" in content
