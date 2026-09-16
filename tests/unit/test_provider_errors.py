"""Provider 错误分类的稳定性测试。"""

from enterprise_rag.providers.errors import ProviderError, ProviderErrorKind


def test_provider_error_keeps_safe_kind_without_raw_upstream_details() -> None:
    """对外只保留稳定分类，调用方不应看到 Authorization 或完整上游 body。"""

    error = ProviderError(
        ProviderErrorKind.AUTHENTICATION,
        "上游认证失败",
        retryable=False,
    )

    assert error.kind is ProviderErrorKind.AUTHENTICATION
    assert "Authorization" not in str(error)
    assert error.retryable is False
