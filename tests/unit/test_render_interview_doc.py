"""文档生成器回归：正文顺序、离线时序图与重复生成的结构稳定性。"""

import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

from scripts.render_interview_doc import InterviewHtmlRenderer, _render_sequence, render_document


class DocumentLinks(HTMLParser):
    """用标准 HTML parser 收集实际锚点与链接，不猜测渲染后的属性转义。"""

    def __init__(self) -> None:
        """初始化当前文档的链接与唯一 ID 集合。"""

        super().__init__()
        self.ids: list[str] = []
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """读取开始标签属性，仅记录导航验证所需数据。"""

        attributes = dict(attrs)
        if attributes.get("id"):
            self.ids.append(attributes["id"])
        if tag == "a" and attributes.get("href"):
            self.links.append(attributes["href"])


def test_chapter_heading_is_preserved_without_duplicate_ids() -> None:
    """章节应保留 h2，锚点只在 section 上出现，重复小标题不生成重复 ID。"""

    output = InterviewHtmlRenderer().render(
        "## 1. 文档入库\n\n### Q1：如何入库？\n\n#### 时序图\n\n"
        "## 2. 查询\n\n### Q2：如何查询？\n\n#### 时序图\n"
    )
    assert output.count('<h2 class="chap-title">') == 2
    assert output.count('<section class="chapter"') == output.count("</section>") == 2
    assert output.count('id="1-文档入库"') == 1
    assert "<h4>时序图</h4>" in output
    assert '<h4 id=' not in output


def test_sequence_diagram_preserves_prefix_and_escapes_text() -> None:
    """条件前缀保留为消息而不是参与方；XML 序列化转义不可信标签。"""

    output = _render_sequence(
        "成功：API -> DB：发布 <pointer>\n失败：API -> API：记录失败\n"
    )
    start = output.index("<svg")
    stop = output.index("</svg>") + len("</svg>")
    root = ET.fromstring(output[start:stop])
    ns = {"s": "http://www.w3.org/2000/svg"}
    assert len(root.findall("s:rect", ns)) == 2
    assert len(root.findall("s:polygon", ns)) == 2
    assert "成功：发布 &lt;pointer&gt;" in output
    assert any(node.attrib.get("class") == "sequence-arrow" for node in root.findall("s:path", ns))


def test_multi_hop_sequence_draws_each_call() -> None:
    """同一行多跳链路应展开为多条调用，不丢失中间参与方。"""

    output = _render_sequence("产品：用户 -> API -> LLM：请求\n")
    assert output.count('class="sequence-actor"') == 3
    assert output.count('class="sequence-arrowhead"') == 2
    assert "产品：请求" in output


def test_teaching_panel_has_diagram_code_and_oral_answer() -> None:
    """每题的五段教学内容按顺序渲染，并保留图的可展开原始文本。"""

    source = (
        "### Q1：示例\n\n#### 先用白话理解\n\n说明\n\n"
        "#### 分步骤看实现\n\n1. 检查\n\n#### 时序图\n\n"
        "```text\nAPI -> DB：保存\n```\n\n"
        "#### 极简核心代码（示意，不是可直接复制的业务代码）\n\n"
        "```python\nawait save()\n```\n\n#### 简版口述回答\n\n回答\n\n---\n\n"
    )
    output = InterviewHtmlRenderer().render(source)
    assert output.count('class="explain-panel"') == 1
    assert output.count('class="sequence"') == 1
    assert output.count('class="code-block micro-code"') == 1
    assert output.count('class="oral-answer"') == 1
    assert '<details class="sequence-source">' in output
    assert output.index("先用白话理解") < output.index('class="sequence"')
    assert output.index('class="sequence"') < output.index("简版口述回答")


def test_regeneration_is_idempotent_and_keeps_preamble(tmp_path: Path) -> None:
    """同日重复生成不积累样式或外壳，也不丢失状态声明与教学约定。"""

    markdown_path = tmp_path / "qa.md"
    html_path = tmp_path / "qa.html"
    markdown_path.write_text(
        "# 问答\n\n**代码阅读约定：** 教学示意\n\n"
        "> 这不是已完成生产系统。\n\n**设计动机声明：** 工程分析\n\n"
        "> **给完全不了解代码的读者：** 术语表\n\n## 目录\n\n"
        "## 1. 入库\n\n### Q1：示例\n",
        encoding="utf-8",
    )
    html_path.write_text(
        '<html><head></head><body>\n  <nav class="toc">\n  </nav>\n'
        '<main>\n    <header class="hero">旧内容</header>\n'
        '<button class="to-top"></button></main></body></html>',
        encoding="utf-8",
    )
    # 第一遍生成，再用第一遍产物作模板；第二遍必须字节一致。
    render_document(markdown_path, html_path)
    first = html_path.read_text(encoding="utf-8")
    render_document(markdown_path, html_path)
    second = html_path.read_text(encoding="utf-8")
    assert first == second
    assert second.count('id="teaching-layout"') == 1
    assert second.count('<header class="hero">') == 1
    assert second.count('<footer class="foot">') == 1
    assert "这不是已完成生产系统。" in second
    assert "设计动机声明" in second
    assert "代码阅读约定" in second


def test_repository_document_has_all_teaching_steps_and_diagrams() -> None:
    """交付正文的 73 题均有逐步代码表，HTML 对应题号与离线图一一匹配。"""

    directory = Path(__file__).resolve().parents[2] / "docs" / "interview"
    markdown = (directory / "advanced-rag-engineering-qa.md").read_text(encoding="utf-8")
    output = (directory / "advanced-rag-engineering-qa.html").read_text(encoding="utf-8")
    questions = re.findall(
        r"^### Q(\d+)：[^\n]+\n(.*?)(?=^### Q\d+：|^## |\Z)",
        markdown,
        re.MULTILINE | re.DOTALL,
    )
    assert [int(number) for number, _ in questions] == list(range(1, 74))
    labels = (
        "先用白话理解", "分步骤看实现", "时序图",
        "极简核心代码（示意，不是可直接复制的业务代码）", "简版口述回答",
    )
    for number, content in questions:
        # 每题教学次序固定，每步表格同时有机制、代码和输出，不能只堆术语。
        locations = [content.index(f"#### {label}") for label in labels]
        assert locations == sorted(locations), number
        steps = content[locations[1]:locations[2]]
        assert "| 极简核心代码（教学示意） |" in steps, number
        rows = re.findall(r"^\| \d+\. .*", steps, re.MULTILINE)
        assert len(rows) >= 3, number
        assert all("`" in row for row in rows), number
    assert len(re.findall(r'<h3 id="q\d+', output)) == 73
    assert output.count('class="explain-panel"') == 73
    assert output.count('class="sequence"') == 73
    assert output.count('class="oral-answer"') == 73
    assert output.count('class="code-block micro-code"') == 73
    assert output.count('<section class="chapter"') == output.count("</section>") == 18
    assert output.count('<h2 class="chap-title">') == 18


def test_repository_document_links_and_svg_are_valid() -> None:
    """所有页内锚点、仓库路径存在，73 个 SVG 均能按 XML 正常解析。"""

    directory = Path(__file__).resolve().parents[2] / "docs" / "interview"
    output = (directory / "advanced-rag-engineering-qa.html").read_text(encoding="utf-8")
    parser = DocumentLinks()
    parser.feed(output)
    assert len(parser.ids) == len(set(parser.ids))
    for href in parser.links:
        url = urlsplit(href)
        if url.scheme or url.netloc:
            continue
        if not url.path and url.fragment:
            assert unquote(url.fragment) in parser.ids, href
        elif url.path:
            assert (directory / unquote(url.path)).exists(), href
    diagrams = re.findall(r"(<svg\b.*?</svg>)", output, re.DOTALL)
    assert len(diagrams) == 73
    for diagram in diagrams:
        root = ET.fromstring(diagram)
        assert root.attrib["role"] == "img"
        assert "sequence-arrowhead" in diagram
