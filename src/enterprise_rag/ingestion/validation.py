"""摄取入口的媒体类型、大小和编码校验。"""

from __future__ import annotations

import hashlib
import unicodedata
from pathlib import PurePosixPath

from enterprise_rag.ingestion.contracts import IngestionRequest, ValidatedRequest


class IngestionValidationError(ValueError):
    """输入不满足摄取契约；错误消息不包含原文正文。"""


class UnsupportedMediaTypeError(IngestionValidationError):
    """扩展名或显式 MIME 不在受控支持范围。"""


class PayloadTooLargeError(IngestionValidationError):
    """请求字节数超过当前入口预算。"""


class InvalidEncodingError(IngestionValidationError):
    """文本文档不是合法 UTF-8。"""


class EmptyDocumentError(IngestionValidationError):
    """文档没有可处理的非空文本。"""


SUPPORTED_MEDIA_TYPES: dict[str, str] = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".html": "text/html",
    ".htm": "text/html",
    ".pdf": "application/pdf",
}


def _extension(filename: str) -> str:
    """只取文件名最后一个后缀，并统一为小写；不接受目录穿越语义。"""

    name = PurePosixPath(filename.replace("\\", "/")).name
    return PurePosixPath(name).suffix.lower()


def _normalized_text(content: bytes) -> str:
    """严格 UTF-8 解码并做 NFC/换行规范化。"""

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidEncodingError("文本文件必须是 UTF-8") from exc
    return unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))


def validate_request(request: IngestionRequest, *, max_bytes: int = 10 * 1024 * 1024) -> ValidatedRequest:
    """校验并规范化请求。

    PDF 不在入口强制 UTF-8，因为其内容由 PDF parser 提取；是否为可提取文本 PDF
    由 parser 再判定。这样错误 PDF 仍可以先保存原文并进入 FAILED 状态，支持显式
    replay，而不会在对象存储和数据库之间留下无法识别的请求。
    """

    if max_bytes <= 0:
        raise ValueError("max_bytes 必须大于 0")
    if not isinstance(request.content, bytes):
        raise IngestionValidationError("content 必须是 bytes")
    if not request.tenant_id.strip() or not request.source_id.strip():
        raise IngestionValidationError("tenant_id 和 source_id 不能为空")
    if not request.title.strip() or not request.filename.strip():
        raise IngestionValidationError("title 和 filename 不能为空")
    if not request.idempotency_key.strip():
        raise IngestionValidationError("idempotency_key 不能为空")
    if len(request.content) > max_bytes:
        raise PayloadTooLargeError("文档超过大小限制")

    extension = _extension(request.filename)
    media_type = SUPPORTED_MEDIA_TYPES.get(extension)
    if media_type is None:
        raise UnsupportedMediaTypeError("不支持的文档格式")
    if request.content_type:
        supplied = request.content_type.split(";", 1)[0].strip().lower()
        if supplied != media_type:
            raise UnsupportedMediaTypeError("文件扩展名与 MIME 类型不匹配")

    text = None if extension == ".pdf" else _normalized_text(request.content)
    if text is not None and not text.strip():
        raise EmptyDocumentError("文档没有可处理文本")

    return ValidatedRequest(
        request=request,
        extension=extension,
        media_type=media_type,
        text=text,
        content_hash=hashlib.sha256(request.content).hexdigest(),
    )


__all__ = [
    "EmptyDocumentError",
    "IngestionValidationError",
    "InvalidEncodingError",
    "PayloadTooLargeError",
    "SUPPORTED_MEDIA_TYPES",
    "UnsupportedMediaTypeError",
    "validate_request",
]
