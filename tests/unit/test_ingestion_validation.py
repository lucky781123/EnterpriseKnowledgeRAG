"""摄取输入校验的单元测试。

这些测试先固定安全边界，再允许生产代码实现具体解析逻辑。测试故意不读取
项目 `.env`，也不把原文内容写入错误消息，避免测试日志泄露敏感信息。
"""

from __future__ import annotations

import pytest

from enterprise_rag.ingestion.contracts import IngestionRequest
from enterprise_rag.ingestion.validation import (
    EmptyDocumentError,
    InvalidEncodingError,
    PayloadTooLargeError,
    UnsupportedMediaTypeError,
    validate_request,
)


def _request(**overrides) -> IngestionRequest:
    """构造一份最小合法请求，单个测试只覆盖自己关心的变化。"""

    values = {
        "tenant_id": "tenant-a",
        "source_id": "git:repo/docs/guide.md",
        "title": "开发指南",
        "filename": "guide.md",
        "content": "# 标题\n\n正文".encode("utf-8"),
        "idempotency_key": "request-1",
    }
    values.update(overrides)
    return IngestionRequest(**values)


def test_markdown_request_is_validated_and_normalized():
    """合法 Markdown 应得到稳定扩展名和媒体类型，而不是依赖调用方猜测。"""

    validated = validate_request(_request(), max_bytes=1024)

    assert validated.extension == ".md"
    assert validated.media_type == "text/markdown"
    assert validated.text == "# 标题\n\n正文"


def test_unknown_extension_is_rejected_before_object_storage():
    """未知格式必须在写对象前失败，避免产生无法重放的半成品对象。"""

    with pytest.raises(UnsupportedMediaTypeError, match="不支持"):
        validate_request(_request(filename="guide.docx"))


def test_explicit_mismatched_content_type_is_rejected():
    """显式 MIME 与扩展名冲突时拒绝请求，防止把二进制误交给文本解析器。"""

    with pytest.raises(UnsupportedMediaTypeError, match="MIME"):
        validate_request(_request(content_type="application/pdf"))


def test_invalid_utf8_is_rejected_without_echoing_payload():
    """文本格式只接受严格 UTF-8；异常消息不能包含原文片段。"""

    secret_bytes = b"\xffprivate-secret"
    with pytest.raises(InvalidEncodingError) as captured:
        validate_request(_request(content=secret_bytes))

    assert "private-secret" not in str(captured.value)


def test_empty_text_and_size_limit_are_explicit_failures():
    """空文档与超限文档都必须有可区分的失败类型。"""

    with pytest.raises(EmptyDocumentError):
        validate_request(_request(content=b"\n\t  "))

    with pytest.raises(PayloadTooLargeError):
        validate_request(_request(), max_bytes=2)

