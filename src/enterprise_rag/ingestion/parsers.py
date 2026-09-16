"""受控文档解析器。

解析器只把输入转换为统一 `ParsedBlock`，不尝试做 OCR、表格恢复或语义推断。
这样每个 block 都能保留可解释的来源 lineage，后续 chunk、索引和引用阶段可以
明确知道哪些信息来自原文、哪些信息是派生结果。
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import unicodedata
from html.parser import HTMLParser
from typing import Any

from PyPDF2 import PdfReader

from enterprise_rag.ingestion.contracts import ParsedBlock
from enterprise_rag.ingestion.validation import (
    EmptyDocumentError,
    InvalidEncodingError,
    SUPPORTED_MEDIA_TYPES,
    UnsupportedMediaTypeError,
)


class ParseError(ValueError):
    """解析器无法从受控格式恢复结构化文本。"""


class EncryptedPdfError(ParseError):
    """PDF 已加密；首期不尝试猜测密码或绕过保护。"""


def _normalise_text(value: str) -> str:
    """统一 Unicode 和换行，并去掉 block 两端无意义空白。"""

    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n")).strip()


def _make_block(
    block_type: str,
    text: str,
    heading_path: tuple[str, ...],
    source_locator: dict[str, Any],
) -> ParsedBlock | None:
    """使用结构化字段计算稳定 block ID；空 block 不进入下游。"""

    normalized = _normalise_text(text)
    if not normalized:
        return None
    identity = {
        "parser_version": "structured-block-v1",
        "block_type": block_type,
        "text": normalized,
        "heading_path": list(heading_path),
        "source_locator": source_locator,
    }
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return ParsedBlock(
        block_id=f"block_{digest}",
        block_type=block_type,
        text=normalized,
        heading_path=heading_path,
        source_locator=dict(source_locator),
    )


def _push_heading(stack: list[tuple[int, str]], level: int, title: str) -> tuple[str, ...]:
    """按 Markdown/HTML 标题级别更新当前标题路径。"""

    while stack and stack[-1][0] >= level:
        stack.pop()
    stack.append((level, title))
    return tuple(item[1] for item in stack)


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_LIST_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")


def _parse_markdown(text: str) -> list[ParsedBlock]:
    """以行扫描方式解析常见 Markdown block，保留稳定 1-based 行范围。"""

    lines = text.split("\n")
    blocks: list[ParsedBlock] = []
    heading_stack: list[tuple[int, str]] = []
    index = 0

    def add(block_type: str, value: str, path: tuple[str, ...], start: int, end: int) -> None:
        block = _make_block(block_type, value, path, {"kind": "line", "start": start, "end": end})
        if block is not None:
            blocks.append(block)

    while index < len(lines):
        line = lines[index]
        line_number = index + 1
        if not line.strip():
            index += 1
            continue

        fence = _FENCE_RE.match(line)
        if fence:
            start = line_number
            marker = fence.group(1)[0]
            index += 1
            code_lines: list[str] = []
            while index < len(lines) and not lines[index].lstrip().startswith(marker * 3):
                code_lines.append(lines[index])
                index += 1
            if index < len(lines):
                end = index + 1
                index += 1
            else:
                # 未闭合 fenced code 仍然是可解释的代码 block；不会把后续内容
                # 猜测成普通段落，调用方可在 locator 中看到它直到文件末尾。
                end = len(lines)
            add("code", "\n".join(code_lines), tuple(item[1] for item in heading_stack), start, end)
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            path = _push_heading(heading_stack, len(heading.group(1)), heading.group(2))
            add("heading", heading.group(2), path, line_number, line_number)
            index += 1
            continue

        if _LIST_RE.match(line):
            start = line_number
            list_lines = [line]
            index += 1
            while index < len(lines) and lines[index].strip() and _LIST_RE.match(lines[index]):
                list_lines.append(lines[index])
                index += 1
            # 去掉项目符号本身，只把条目正文交给下游；原始行范围仍保留在
            # locator 中，因此清洗不会损失回溯能力。
            list_text = "\n".join(_LIST_RE.sub("", item, count=1).strip() for item in list_lines)
            add("list", list_text, tuple(item[1] for item in heading_stack), start, index)
            continue

        start = line_number
        paragraph = [line]
        index += 1
        while index < len(lines):
            candidate = lines[index]
            if not candidate.strip() or _HEADING_RE.match(candidate) or _LIST_RE.match(candidate) or _FENCE_RE.match(candidate):
                break
            paragraph.append(candidate)
            index += 1
        add("paragraph", "\n".join(paragraph), tuple(item[1] for item in heading_stack), start, index)

    if not blocks:
        raise EmptyDocumentError("文档没有可处理文本")
    return blocks


class _HTMLBlockParser(HTMLParser):
    """HTMLParser 的轻量 block 收集器，只保留可靠的行定位。"""

    _HEADING_TAGS = {f"h{i}": i for i in range(1, 7)}
    _BLOCK_TAGS = set(_HEADING_TAGS) | {"p", "li", "pre"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[ParsedBlock] = []
        self.heading_stack: list[tuple[int, str]] = []
        self.active: dict[str, Any] | None = None
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        tag = tag.lower()
        if tag in {"script", "style", "noscript"}:
            self.ignored_depth += 1
            return
        if self.ignored_depth or self.active is not None or tag not in self._BLOCK_TAGS:
            return
        self.active = {"tag": tag, "start": self.getpos()[0], "parts": []}

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self.ignored_depth == 0 and self.active is not None:
            self.active["parts"].append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"}:
            self.ignored_depth = max(0, self.ignored_depth - 1)
            return
        if self.ignored_depth or self.active is None or self.active["tag"] != tag:
            return
        active = self.active
        self.active = None
        text = _normalise_text("".join(active["parts"]))
        if not text:
            return
        if tag in self._HEADING_TAGS:
            path = _push_heading(self.heading_stack, self._HEADING_TAGS[tag], text)
            block_type = "heading"
        elif tag == "li":
            path = tuple(item[1] for item in self.heading_stack)
            block_type = "list"
        elif tag == "pre":
            path = tuple(item[1] for item in self.heading_stack)
            block_type = "code"
        else:
            path = tuple(item[1] for item in self.heading_stack)
            block_type = "paragraph"
        block = _make_block(
            block_type,
            text,
            path,
            {"kind": "line", "start": active["start"], "end": self.getpos()[0]},
        )
        if block is not None:
            self.blocks.append(block)

    def close(self) -> None:
        super().close()
        if self.active is not None:
            # HTML 允许省略结束标签；把未闭合 block 的结束行限定为最后一次
            # 看到的起始行，避免伪造一个未知的精确范围。
            active = self.active
            self.active = None
            text = _normalise_text("".join(active["parts"]))
            if text:
                tag = active["tag"]
                if tag in self._HEADING_TAGS:
                    path = _push_heading(self.heading_stack, self._HEADING_TAGS[tag], text)
                    block_type = "heading"
                elif tag == "li":
                    path = tuple(item[1] for item in self.heading_stack)
                    block_type = "list"
                elif tag == "pre":
                    path = tuple(item[1] for item in self.heading_stack)
                    block_type = "code"
                else:
                    path = tuple(item[1] for item in self.heading_stack)
                    block_type = "paragraph"
                block = _make_block(
                    block_type,
                    text,
                    path,
                    {"kind": "line", "start": active["start"], "end": active["start"]},
                )
                if block is not None:
                    self.blocks.append(block)


def _parse_html(text: str) -> list[ParsedBlock]:
    """解析 HTML，并把 parser 异常归一化为安全的 ParseError。"""

    parser = _HTMLBlockParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:
        raise ParseError("HTML 解析失败") from exc
    if not parser.blocks:
        raise EmptyDocumentError("HTML 没有可处理文本")
    return parser.blocks


def _parse_pdf(data: bytes) -> list[ParsedBlock]:
    """只读取 PDF 可提取文字，并为每个非空页建立 1-based 页码 locator。"""

    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:
        raise ParseError("PDF 解析失败") from exc
    if getattr(reader, "is_encrypted", False):
        raise EncryptedPdfError("加密 PDF 不在首期支持范围")

    blocks: list[ParsedBlock] = []
    for page_number, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:
            raise ParseError("PDF 文本提取失败") from exc
        block = _make_block("page", text, (), {"kind": "page", "page": page_number})
        if block is not None:
            blocks.append(block)
    if not blocks:
        raise EmptyDocumentError("PDF 没有可提取文本")
    return blocks


def parse_document(data: bytes, *, filename: str, content_type: str | None = None) -> list[ParsedBlock]:
    """根据受控扩展名解析文档；不支持格式在进入 parser 前立即拒绝。"""

    del content_type  # 扩展名已由入口校验绑定 MIME；parser 不重复猜测媒体类型。
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    extension = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if extension not in SUPPORTED_MEDIA_TYPES:
        raise UnsupportedMediaTypeError("不支持的文档格式")
    if extension == ".pdf":
        return _parse_pdf(data)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidEncodingError("文本文件必须是 UTF-8") from exc
    if extension in {".md", ".markdown"}:
        return _parse_markdown(text)
    return _parse_html(text)


__all__ = [
    "EmptyDocumentError",
    "EncryptedPdfError",
    "ParseError",
    "parse_document",
]
