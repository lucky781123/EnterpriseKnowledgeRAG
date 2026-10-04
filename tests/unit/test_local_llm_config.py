"""本地回答模型文件配置测试；使用临时TOML，不访问真实模型服务。"""

import tomllib
from pathlib import Path

import pytest

from enterprise_rag.config.settings import Settings


def test_file_configuration_overrides_legacy_environment(monkeypatch, tmp_path):
    """显式文件优先于旧环境变量，下一次读取即可采用文件中的新模型。"""
    path = tmp_path / "answer.toml"
    path.write_text(
        'model = "first-model"\nbase_url = "http://127.0.0.1:1234/v1"\n'
        'max_concurrency = 1\nmax_queue_size = 4\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("LOCAL_LLM_CONFIG_FILE", str(path))
    monkeypatch.setenv("LOCAL_LLM_MODEL", "stale-model")
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://unused:9999")
    monkeypatch.setenv("LOCAL_LLM_MAX_CONCURRENCY", "invalid-stale-value")
    settings = Settings.from_env()
    assert settings.local_llm_model == "first-model"
    assert settings.local_llm_base_url == "http://127.0.0.1:1234/v1"
    assert settings.local_llm_max_concurrency == 1
    assert settings.local_llm_max_queue_size == 4
    # 模拟用户只修改配置文件，重新启动时重新读取，不缓存旧模型。
    path.write_text(path.read_text().replace("first-model", "second-model"), encoding="utf-8")
    assert Settings.from_env().local_llm_model == "second-model"


def test_legacy_environment_remains_supported(monkeypatch):
    """没有启用文件时，旧部署继续从环境变量读取模型。"""
    monkeypatch.delenv("LOCAL_LLM_CONFIG_FILE", raising=False)
    monkeypatch.setenv("LOCAL_LLM_MODEL", "legacy-model")
    assert Settings.from_env().local_llm_model == "legacy-model"


def test_missing_configuration_does_not_fall_back(monkeypatch, tmp_path):
    """显式配置文件不存在时拒绝启动，不能偷偷使用旧模型。"""
    monkeypatch.setenv("LOCAL_LLM_CONFIG_FILE", str(tmp_path / "missing.toml"))
    with pytest.raises(ValueError, match="LOCAL_LLM_CONFIG_FILE"):
        Settings.from_env()


@pytest.mark.parametrize(
    "contents",
    [
        'model = ""\nbase_url = "http://127.0.0.1:1234"\n',
        'model = 123\nbase_url = "http://127.0.0.1:1234"\n',
        'model = "model"\nbase_url = ""\n',
        'model = "model"\n',
        'model = "model"\nbase_url = "http://127.0.0.1:1234"\nmax_concurrency = true\n',
        'model = "model"\nbase_url = "http://127.0.0.1:1234"\nmax_queue_size = 0\n',
        'model = "model"\nbase_url = "http://127.0.0.1:1234"\napi_key = "private"\n',
        'model = [\n',
    ],
)
def test_invalid_configuration_is_rejected(monkeypatch, tmp_path, contents):
    """拒绝空模型、错误类型、未知字段、非法TOML，不输出配置正文。"""
    path = tmp_path / "invalid.toml"
    path.write_text(contents, encoding="utf-8")
    monkeypatch.setenv("LOCAL_LLM_CONFIG_FILE", str(path))
    with pytest.raises(ValueError) as error:
        Settings.from_env()
    assert "private" not in str(error.value)


def test_relative_file_path_is_project_relative(monkeypatch, tmp_path):
    """从其他工作目录读取时，项目相对路径仍定位同一份配置。"""
    monkeypatch.setenv("LOCAL_LLM_CONFIG_FILE", "config/local-llm.toml")
    monkeypatch.chdir(tmp_path)
    # 用户可以任意修改真实配置中的模型，测试不把当前模型ID冻结成代码常量。
    path = Path(__file__).resolve().parents[2] / "config/local-llm.toml"
    expected = tomllib.loads(path.read_text(encoding="utf-8"))["model"]
    assert Settings.from_env().local_llm_model == expected


def test_embedding_and_reranker_file_overrides_environment(monkeypatch, tmp_path):
    """同一文件配置三类模型，旧环境中的无效维度也不会覆盖文件。"""
    path = tmp_path / "models.toml"
    path.write_text(
        'model="answer"\nbase_url="http://localhost:1234"\n'
        '[embedding]\nmodel="new-embed"\nbase_url="http://localhost:11434"\ndimension=2560\n'
        '[reranker]\nmodel="new-rerank"\nbase_url="http://localhost:8003/v1"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("LOCAL_LLM_CONFIG_FILE", str(path))
    monkeypatch.setenv("OLLAMA_EMBEDDING_MODEL", "old")
    monkeypatch.setenv("OLLAMA_EMBEDDING_DIMENSION", "invalid-stale")
    monkeypatch.setenv("BGE_RERANKER_MODEL", "old")
    settings = Settings.from_env()
    assert settings.ollama_embedding_model == "new-embed"
    assert settings.ollama_embedding_dimension == 2560
    assert settings.ollama_base_url == "http://localhost:11434"
    assert settings.reranker_model == "new-rerank"
    assert settings.reranker_base_url == "http://localhost:8003/v1"
    assert settings.reranker_provider == "http"
    assert settings.dense_profile_namespace


@pytest.mark.parametrize("section", [
    '[embedding]\nmodel="embed"\nbase_url="http://localhost"\ndimension=true',
    '[embedding]\nmodel="embed"\nbase_url="http://localhost"\ndimension=0',
    '[embedding]\nmodel="embed"\nbase_url="http://localhost"\ndimension=2\napi_key="secret"',
    '[embedding]\nmodel="embed"\nbase_url="http://localhost"',
    '[reranker]\nmodel=""\nbase_url="http://localhost"',
    '[reranker]\nmodel="rerank"\nbase_url=""',
    '[reranker]\nmodel="rerank"\nbase_url="http://localhost"\napi_key="secret"',
])
def test_invalid_model_sections_fail_closed(monkeypatch, tmp_path, section):
    """嵌套表缺字段、非法维度、未知密钥字段必须在启动时拒绝。"""
    path = tmp_path / "invalid.toml"
    path.write_text('model="answer"\nbase_url="http://localhost"\n' + section, encoding="utf-8")
    monkeypatch.setenv("LOCAL_LLM_CONFIG_FILE", str(path))
    with pytest.raises(ValueError) as error:
        Settings.from_env()
    assert "secret" not in str(error.value)
