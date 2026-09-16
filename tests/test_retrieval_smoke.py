"""真实检索 smoke runner 的纯逻辑契约测试。

这些测试不连接 PostgreSQL、Ollama 或 LM Studio，只锁定临时数据库 URL、
扩展状态和实验报告的安全边界。真实依赖证据必须由独立 runner 产生。
"""

from sqlalchemy.engine import make_url

from scripts.retrieval_smoke import (
    _base_report,
    _normalize_vector_extension,
    _redact_report,
    _render_database_url,
)


def test_render_database_url_preserves_password_when_replacing_database() -> None:
    """替换数据库名时必须保留密码，不能把 SQLAlchemy 的 ``***`` 传给驱动。"""

    base_url = make_url("postgresql+asyncpg://rag:secret@127.0.0.1:54329/enterprise_rag")

    rendered = _render_database_url(base_url, "enterprise_rag_smoke_123")

    parsed = make_url(rendered)
    assert parsed.database == "enterprise_rag_smoke_123"
    assert parsed.password == "secret"


def test_normalize_vector_extension_distinguishes_available_and_installed() -> None:
    """只读探测必须区分“可安装”和“已安装”，不能把 available 冒充 installed。"""

    assert _normalize_vector_extension("0.8.0", None) == {
        "available": True,
        "installed": False,
        "available_version": "0.8.0",
        "installed_version": None,
    }
    assert _normalize_vector_extension("0.8.0", "0.8.0") == {
        "available": True,
        "installed": True,
        "available_version": "0.8.0",
        "installed_version": "0.8.0",
    }
    assert _normalize_vector_extension(None, None) == {
        "available": False,
        "installed": False,
        "available_version": None,
        "installed_version": None,
    }


def test_redact_report_removes_nested_credentials_and_model_body() -> None:
    """实验报告必须递归移除凭据和完整模型正文，同时保留安全的状态字段。"""

    report = {
        "postgres_dsn": "postgresql://rag:secret@127.0.0.1/db",
        "api_key": "top-secret",
        "llm": {
            "answer": "这里是完整模型回答",
            "message": "这里也是模型正文",
            "status": "completed",
            "citation_validation": "passed",
        },
        "nested": [{"password": "secret", "safe": "kept"}],
    }

    redacted = _redact_report(report)

    assert redacted["postgres_dsn"] == "[REDACTED]"
    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["llm"]["answer"] == "[REDACTED]"
    assert redacted["llm"]["message"] == "[REDACTED]"
    assert redacted["llm"]["status"] == "completed"
    assert redacted["llm"]["citation_validation"] == "passed"
    assert redacted["nested"] == [{"password": "[REDACTED]", "safe": "kept"}]


def test_base_report_marks_postgres_json_path_as_non_production_evidence() -> None:
    """runner 的基础报告必须固定声明当前后端和禁止外推的结论。"""

    report = _base_report()

    assert report["vector_backend"] == "postgres_json_application_cosine"
    assert report["production_performance_claim"] is False
    assert report["retrieval_quality_claim"] is False

