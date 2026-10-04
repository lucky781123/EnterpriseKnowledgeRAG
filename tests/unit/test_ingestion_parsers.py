"""Markdown、HTML 和受控文本 PDF 解析器的单元测试。"""

from __future__ import annotations

import json

import pytest

from enterprise_rag.ingestion.contracts import ParsedBlock
from enterprise_rag.ingestion.parsers import (
    EncryptedPdfError,
    EmptyDocumentError,
    parse_document,
)


def test_markdown_parser_keeps_heading_path_and_lineage():
    """Markdown 的标题路径和行范围应能让下游回到原文位置。"""

    source = (
        "# 安装\n"
        "\n"
        "先准备环境。\n"
        "\n"
        "- 安装依赖\n"
        "- 运行测试\n"
        "\n"
        "```python\n"
        "print('ok')\n"
        "```\n"
    ).encode()

    blocks = parse_document(source, filename="guide.md")

    assert [block.block_type for block in blocks] == ["heading", "paragraph", "list", "code"]
    assert blocks[1].heading_path == ("安装",)
    assert blocks[2].source_locator == {"kind": "line", "start": 5, "end": 6}
    assert blocks[3].source_locator == {"kind": "line", "start": 8, "end": 10}
    assert all(block.block_id for block in blocks)


def test_txt_preserves_literal_markdown_and_html_with_lineage() -> None:
    """TXT不能删除Markdown符号或HTML样式文本，保留行范围和确定性identity。"""

    source = "# 不是标题\n<b>字面文本</b>\n\n第二段\n".encode()
    blocks = parse_document(source, filename="guide.txt")
    assert [block.text for block in blocks] == ["# 不是标题\n<b>字面文本</b>", "第二段"]
    assert blocks[0].heading_path == ()
    assert blocks[0].source_locator == {"kind": "line", "start": 1, "end": 2}
    assert blocks == parse_document(source, filename="guide.txt")


def test_html_parser_tracks_heading_path_and_stable_order_without_fake_offsets():
    """HTML 解析只记录可靠的行范围，不臆造字符 offset。"""

    source = (
        "<html>\n"
        "<body>\n"
        "<h1>部署</h1>\n"
        "<p>先安装依赖。</p>\n"
        "<ul><li>启动服务</li><li>检查健康状态</li></ul>\n"
        "</body>\n"
        "</html>\n"
    ).encode()

    blocks = parse_document(source, filename="guide.html")

    assert [block.block_type for block in blocks] == ["heading", "paragraph", "list", "list"]
    assert blocks[1].heading_path == ("部署",)
    assert blocks[2].text == "启动服务"
    assert blocks[2].source_locator["kind"] == "line"
    assert "offset" not in blocks[2].source_locator


class _FakePage:
    """用于隔离 PyPDF2 版本差异的最小 PDF page double。"""

    def __init__(self, text: str):
        self._text = text

    def extract_text(self):
        return self._text


class _FakeReader:
    """可控制加密标志和页文本的 PdfReader double。"""

    def __init__(self, _stream):
        self.is_encrypted = False
        self.pages = [_FakePage("第一页文本"), _FakePage(""), _FakePage("第三页文本")]


def test_pdf_parser_records_one_based_page_lineage(monkeypatch):
    """PDF 只承诺页码 lineage，不把 PDF 内部字节偏移冒充字符位置。"""

    monkeypatch.setattr("enterprise_rag.ingestion.parsers.PdfReader", _FakeReader)

    blocks = parse_document(b"%PDF-fake", filename="manual.pdf")

    assert [block.text for block in blocks] == ["第一页文本", "第三页文本"]
    assert [block.source_locator for block in blocks] == [
        {"kind": "page", "page": 1},
        {"kind": "page", "page": 3},
    ]


def test_encrypted_and_empty_pdf_fail_explicitly(monkeypatch):
    """加密 PDF 和没有可提取文字的 PDF 都不能伪装成成功。"""

    class EncryptedReader(_FakeReader):
        def __init__(self, stream):
            super().__init__(stream)
            self.is_encrypted = True

    monkeypatch.setattr("enterprise_rag.ingestion.parsers.PdfReader", EncryptedReader)
    with pytest.raises(EncryptedPdfError):
        parse_document(b"%PDF-encrypted", filename="secret.pdf")

    class EmptyReader(_FakeReader):
        def __init__(self, stream):
            super().__init__(stream)
            self.pages = [_FakePage("  ")]

    monkeypatch.setattr("enterprise_rag.ingestion.parsers.PdfReader", EmptyReader)
    with pytest.raises(EmptyDocumentError):
        parse_document(b"%PDF-empty", filename="empty.pdf")


def test_real_text_pdf_sample_is_extracted_with_page_lineage():
    """使用最小真实 PDF 字节样本验证 PyPDF2 文本提取，而不只依赖 fake reader。"""

    # 该样本只包含一个 Helvetica 文本页，offset 由测试内 helper 动态计算，
    # 避免把 PDF 二进制偏移硬编码到业务 parser；它代表首期“受控文本 PDF”边界。
    objects = [
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n",
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj\n",
        b"4 0 obj << /Length 44 >> stream\nBT /F1 12 Tf 72 720 Td (Hello PDF) Tj ET\nendstream endobj\n",
        b"5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n",
    ]
    body = b"%PDF-1.4\n" + b"".join(objects)
    offsets = []
    cursor = len(b"%PDF-1.4\n")
    for obj in objects:
        offsets.append(cursor)
        cursor += len(obj)
    xref_offset = len(body)
    xref = b"xref\n0 6\n0000000000 65535 f \n" + b"".join(
        f"{offset:010d} 00000 n \n".encode() for offset in offsets
    )
    trailer = (
        b"trailer << /Size 6 /Root 1 0 R >>\n"
        b"startxref\n"
        + str(xref_offset).encode()
        + b"\n%%EOF\n"
    )

    blocks = parse_document(body + xref + trailer, filename="sample.pdf")

    assert len(blocks) == 1
    assert "Hello PDF" in blocks[0].text
    assert blocks[0].source_locator == {"kind": "page", "page": 1}


def test_parsed_blocks_are_json_serializable():
    """中间表示必须能稳定落为 JSON，供 parsed artifact 重放。"""

    block = ParsedBlock(
        block_id="block-1",
        block_type="paragraph",
        text="正文",
        heading_path=("章节",),
        source_locator={"kind": "line", "start": 1, "end": 1},
    )

    encoded = json.dumps(block.to_dict(), ensure_ascii=False, sort_keys=True)

    assert '"heading_path": ["章节"]' in encoded
