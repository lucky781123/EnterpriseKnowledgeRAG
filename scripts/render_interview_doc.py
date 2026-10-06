"""把 RAG 面试 Markdown 渲染为带导航和主题切换的单文件 HTML。

本脚本只用于文档展示，不属于在线 API 或入库链路。它复用已经存在的 HTML
模板外壳，重新渲染正文和侧栏目录，保证 Markdown 与 HTML 不会长期漂移。
"""

from __future__ import annotations

import argparse
import html
import re
import unicodedata
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from markdown_it import MarkdownIt
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name, guess_lexer
from pygments.util import ClassNotFound

PYGMENTS_FORMATTER = HtmlFormatter(nowrap=True)

TEACHING_STYLES = """<style id="teaching-layout">
/* 表格、代码与时序图各自滚动，不能撑宽整页或遮住后续正文。 */
.table-scroll { max-width: 100%; overflow-x: auto; margin: 16px 0; }
.explain-panel .doc-table { min-width: 1000px; table-layout: fixed; }
.explain-panel .doc-table th:first-child { width: 13%; }
.explain-panel .doc-table th:nth-child(2) { width: 35%; }
.explain-panel .doc-table th:nth-child(3) { width: 30%; }
.explain-panel .doc-table th:nth-child(4) { width: 22%; }
.explain-panel .doc-table td { overflow-wrap: anywhere; }
.explain-panel .sequence {
  margin: 14px 0; padding: 0; border: 0; background: var(--surface);
  color: var(--text); font: inherit; white-space: normal; max-width: 100%;
}
.sequence-scroll { overflow-x: auto; max-width: 100%; }
.sequence-diagram { display: block; max-width: none; font-family: inherit; }
.sequence-actor { fill: var(--surface-2); stroke: var(--a3); stroke-width: 1; }
.sequence-label { fill: var(--text); font-size: 12px; font-weight: 600; }
.sequence-message { fill: var(--text); font-size: 13px; }
.sequence-lifeline { stroke: var(--muted); stroke-width: 1; stroke-dasharray: 5 5; opacity: .5; }
.sequence-arrow { fill: none; stroke: var(--a3); stroke-width: 1.8; }
.sequence-arrowhead { fill: var(--a3); }
.sequence-source { margin: 8px 0 16px; }
.sequence-source summary { cursor: pointer; color: var(--a3); font-size: 13px; }
.explain-panel .micro-code { margin: 12px 0; padding: 0; overflow: hidden; white-space: normal; }
.explain-panel .micro-code pre { white-space: pre; overflow-x: auto; }
.chapter .chap-title { scroll-margin-top: 85px; }
@media (max-width: 600px) {
  .explain-panel { padding: 14px 12px; }
  .meta-row { min-width: 0; }
  .meta-val { min-width: 0; overflow-wrap: anywhere; }
}
</style>"""


ACCENTS = (
    "var(--a1)",
    "var(--a2)",
    "var(--a3)",
    "var(--a4)",
    "var(--a5)",
    "var(--a6)",
    "var(--a7)",
    "var(--a8)",
)


def slugify(value: str) -> str:
    """生成与旧页面兼容的稳定锚点，保留中文和 ASCII 字母数字。"""

    normalized = value.strip().lower()
    normalized = re.sub(r"[\s/]+", "-", normalized)
    normalized = re.sub(r"[^\w\-\u4e00-\u9fff]", "", normalized)
    return normalized.strip("-")


class InterviewHtmlRenderer:
    """用 markdown-it 渲染正文，并补齐项目现有页面需要的 HTML 类名。"""

    def __init__(self) -> None:
        """创建支持表格、删除线和 fenced code 的 Markdown 渲染器。"""

        self.markdown = MarkdownIt("commonmark", {"html": True, "linkify": True})
        self.markdown.enable(["table", "strikethrough"])
        self.markdown.add_render_rule("fence", self._render_fence)
        self.markdown.add_render_rule("code_block", self._render_fence)
        self.markdown.add_render_rule("code_inline", self._render_inline_code)

    def render(self, markdown_text: str) -> str:
        """渲染完整正文，并把每道题的初学者解析区包成统一样式面板。"""

        body = self.markdown.render(markdown_text)
        body = self._decorate_tables(body)
        body = self._decorate_links(body)
        body = self._decorate_headings(body)
        body = self._decorate_explain_panels(body)
        body = self._decorate_sections(body)
        return body

    def _render_fence(
        self,
        tokens: list[Any],
        index: int,
        *args: object,
        **kwargs: object,
    ) -> str:
        """使用 Pygments 高亮 fenced code，并保留可复制的纯文本节点。"""

        del args, kwargs
        token = tokens[index]
        language = (token.info or "text").split()[0]
        code = token.content
        try:
            lexer = get_lexer_by_name(language)
        except ClassNotFound:
            lexer = guess_lexer(code)
        highlighted = highlight(code, lexer, PYGMENTS_FORMATTER)
        escaped_language = html.escape(language)
        return (
            f'<div class="code-block" data-lang="{escaped_language}">'
            f'<div class="code-head"><span class="code-lang">{escaped_language}</span>'
            '<button class="code-copy" type="button">复制</button></div>'
            f'<pre class="highlight"><code>{highlighted}</code></pre>'
            "</div>\n"
        )

    def _render_inline_code(
        self,
        tokens: list[Any],
        index: int,
        *args: object,
        **kwargs: object,
    ) -> str:
        """单独渲染行内代码，避免误改 fenced code 内部的 pygments 节点。"""

        del args, kwargs
        token = tokens[index]
        return f'<code class="inline-code">{html.escape(token.content)}</code>'

    @staticmethod
    def _decorate_tables(body: str) -> str:
        """为宽表格增加横向滚动容器，避免移动端文字互相覆盖。"""

        body = body.replace("<table>", '<div class="table-scroll"><table class="doc-table">')
        return body.replace("</table>", "</table></div>")

    @staticmethod
    def _decorate_links(body: str) -> str:
        """区分仓库内相对路径、页内锚点和外部链接。"""

        pattern = re.compile(r'<a href="([^"]+)">')

        def replace(match: re.Match[str]) -> str:
            href = match.group(1)
            if href.startswith("#") and not href.startswith("#q"):
                href = "#" + slugify(unquote(href[1:]))
            css_class = "link"
            if href.startswith(("http://", "https://")):
                css_class += " link--ext"
            elif href.startswith("#"):
                css_class += " link--anchor"
            else:
                css_class += " link--path"
            return f'<a class="{css_class}" href="{href}">'

        return pattern.sub(replace, body)

    @staticmethod
    def _decorate_headings(body: str) -> str:
        """给标准 HTML 标题补上页面类名和稳定锚点。"""

        def replace(level: int, match: re.Match[str]) -> str:
            content = match.group(1)
            title = re.sub(r"<[^>]+>", "", content).strip()
            anchor = _heading_id(title)
            if level == 2:
                return f'<h2 class="chap-title" id="{anchor}">{content}</h2>'
            if level == 3:
                return f'<h3 id="{anchor}">{content}</h3>'
            return f"<h4>{content}</h4>"

        for level in (4, 3, 2):
            body = re.sub(
                rf"<h{level}>(.*?)</h{level}>",
                lambda match, level=level: replace(level, match),
                body,
                flags=re.DOTALL,
            )
        return body

    @staticmethod
    def _decorate_explain_panels(body: str) -> str:
        """把每个 Q 标题到水平分隔线之间的五段解析内容包成统一面板。"""

        pattern = re.compile(
            r'(<h3 id="q\d+[^"]*">.*?</h3>)(.*?)(<hr\s*/>)',
            flags=re.DOTALL,
        )

        def replace(match: re.Match[str]) -> str:
            heading, detail, divider = match.groups()
            # 让时序图和教学代码使用模板中预留的横向滚动样式。
            def sequence(match: re.Match[str]) -> str:
                """从时序文本生成离线 SVG，原始文本仍可展开核对。"""

                title, block, escaped_text = match.groups()
                diagram = _render_sequence(html.unescape(escaped_text))
                return (
                    title + diagram
                    + '<details class="sequence-source"><summary>原始时序文本</summary>'
                    + block + "</details>\n"
                )

            detail = re.sub(
                r'(<h4>时序图</h4>\s*)'
                r'(<div class="code-block" data-lang="text">.*?'
                r"<code>(.*?)</code></pre></div>)",
                sequence,
                detail,
                count=1,
                flags=re.DOTALL,
            )
            detail = re.sub(
                r'(<h4>极简核心代码（示意，不是可直接复制的业务代码）</h4>.*?)'
                r'<div class="code-block" data-lang="python">',
                r'\1<div class="code-block micro-code" data-lang="python">',
                detail,
                count=1,
                flags=re.DOTALL,
            )
            detail = re.sub(
                r'(<h4>简版口述回答</h4>\s*)<p>',
                r'\1<p class="oral-answer">',
                detail,
                count=1,
            )
            return (
                f"{heading}\n<div class=\"explain-panel\">\n{detail.strip()}\n</div>\n{divider}"
            )

        return pattern.sub(replace, body)

    @staticmethod
    def _decorate_sections(body: str) -> str:
        """给二级章节写入稳定 id 和轮换强调色。"""

        chapter_index = 0

        def replace(match: re.Match[str]) -> str:
            nonlocal chapter_index
            content = match.group(1)
            title = html.unescape(re.sub(r"<[^>]+>", "", content)).strip()
            anchor = _heading_id(title)
            accent = ACCENTS[chapter_index % len(ACCENTS)]
            chapter_index += 1
            return (
                f'<section class="chapter" id="{anchor}" '
                f'style="--accent:{accent}">'
                f'<h2 class="chap-title">{content}</h2>'
            )

        decorated = re.sub(
            r'<h2 class="chap-title"[^>]*>(.*?)</h2>',
            replace,
            body,
            flags=re.DOTALL,
        )
        first_section = decorated.find('<section class="chapter"')
        if first_section < 0:
            return decorated

        head = decorated[:first_section]
        chapters = decorated[first_section:]
        chapters = re.sub(
            r'(?<!^)(<section class="chapter")',
            r"</section>\n\1",
            chapters,
        )
        return head + chapters.rstrip() + "\n</section>"


def _render_sequence(source: str) -> str:
    """按时序文本的出现顺序画调用图；分支和规划前缀保留在箭头说明中。"""

    participants: list[str] = []
    messages: list[tuple[str, str, str]] = []
    for raw_line in source.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "->" not in line:
            messages.append(("", "", line))
            continue
        # 冒号在箭头前表示“产品/未来/条件”等前缀，不能误认成参与方名字。
        prefix = ""
        colon = re.search(r"[：:]", line)
        if colon and colon.start() < line.index("->"):
            prefix, line = line[:colon.start()], line[colon.end():]
        path, separator, label = line.partition("：")
        if not separator:
            path, separator, label = line.partition(":")
        chain = [name.strip() for name in path.split("->")]
        for sender, receiver in pairwise(chain):
            for name in (sender, receiver):
                if name not in participants:
                    participants.append(name)
            description = label.strip() or "传递结果"
            if prefix:
                description = f"{prefix}：{description}"
            messages.append((sender, receiver, description))

    column_width = 190
    width = max(580, len(participants) * column_width + 180)
    height = 130 + len(messages) * 100
    root = ET.Element("svg", {
        "xmlns": "http://www.w3.org/2000/svg", "role": "img",
        "aria-label": "按编号自上而下阅读的调用时序图",
        "class": "sequence-diagram", "viewBox": f"0 0 {width} {height}",
        "width": str(width), "height": str(height),
    })
    ET.SubElement(root, "title").text = "调用时序图"
    ET.SubElement(root, "desc").text = source

    def add_text(x: float, y: float, value: str, *, css: str, wrap: int = 40) -> None:
        """按固定列宽分行，长参与方和消息不会遮挡相邻文字。"""

        element = ET.SubElement(root, "text", {
            "x": str(x), "y": str(y), "text-anchor": "middle", "class": css,
        })
        # 中文按两个显示单元计算，ASCII 按一个，固定字号下控制真实文本宽度。
        lines: list[str] = []
        current = ""
        used = 0
        for character in value:
            size = 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
            if current and used + size > wrap:
                lines.append(current)
                current, used = "", 0
            current += character
            used += size
        if current:
            lines.append(current)
        for index, part in enumerate(lines):
            span = ET.SubElement(element, "tspan", {
                "x": str(x), "dy": "0" if index == 0 else "18",
            })
            span.text = part

    positions = {name: 125 + index * column_width for index, name in enumerate(participants)}
    for name, x in positions.items():
        ET.SubElement(root, "rect", {
            "x": str(x - 82), "y": "12", "width": "164", "height": "66",
            "rx": "4", "class": "sequence-actor",
        })
        add_text(x, 36, name, css="sequence-label", wrap=22)
        ET.SubElement(root, "line", {
            "x1": str(x), "y1": "78", "x2": str(x), "y2": str(height - 16),
            "class": "sequence-lifeline",
        })
    for index, (sender, receiver, label) in enumerate(messages, start=1):
        y = 140 + (index - 1) * 100
        if not sender:
            add_text(width / 2, y, f"{index}. {label}", css="sequence-label", wrap=70)
            continue
        x1, x2 = positions[sender], positions[receiver]
        if x1 == x2:
            # 自调用以回折线表示，箭头仍回到原参与方生命线。
            ET.SubElement(root, "path", {
                "d": f"M {x1} {y} h 55 v 32 h -55",
                "class": "sequence-arrow",
            })
            arrow_x, arrow_y, direction = x2, y + 32, -1
            label_x = x1 + 40
        else:
            ET.SubElement(root, "line", {
                "x1": str(x1), "y1": str(y), "x2": str(x2), "y2": str(y),
                "class": "sequence-arrow",
            })
            arrow_x, arrow_y = x2, y
            direction = 1 if x2 > x1 else -1
            label_x = (x1 + x2) / 2
        ET.SubElement(root, "polygon", {
            "points": (
                f"{arrow_x},{arrow_y} "
                f"{arrow_x - direction * 9},{arrow_y - 5} "
                f"{arrow_x - direction * 9},{arrow_y + 5}"
            ),
            "class": "sequence-arrowhead",
        })
        add_text(label_x, y - 42, f"{index}. {label}", css="sequence-message", wrap=36)
    diagram = ET.tostring(root, encoding="unicode", short_empty_elements=False)
    return f'<figure class="sequence"><div class="sequence-scroll">{diagram}</div></figure>\n'


def _heading_id(title: str) -> str:
    """兼容题目中的 Q 前缀，使目录链接与 markdown-it 生成的 id 相同。"""

    normalized = title.strip().lower()
    question = re.match(r"q\s*(\d+)[：:\s]*(.*)", normalized)
    if question:
        number = question.group(1)
        rest = slugify(question.group(2))
        return f"q{number}{rest}"
    return slugify(title)


def _build_sidebar(headings: list[tuple[int, str]]) -> str:
    """按二级章节分组生成可搜索的侧栏目录。"""

    groups: list[dict[str, Any]] = []
    for level, title in headings:
        if level == 2:
            groups.append({"title": title, "id": _heading_id(title), "children": []})
        elif level == 3 and groups:
            groups[-1]["children"].append((title, _heading_id(title)))

    chunks: list[str] = []
    for index, group in enumerate(groups, start=1):
        title = html.escape(group["title"])
        anchor = html.escape(group["id"])
        children = group["children"]
        if children:
            items = "".join(
                f'<li><a href="#{html.escape(child_id)}">'
                f'<span class="toc-badge">{html.escape(child_title.split("：", 1)[0])}</span>'
                f'<span class="toc-text">{html.escape(child_title.split("：", 1)[-1])}</span>'
                "</a></li>"
                for child_title, child_id in children
            )
            chunks.append(
                f'<div class="toc-group has-children" data-slug="{anchor}">'
                f'<div class="toc-row"><a class="toc-h2" href="#{anchor}">'
                f'<span class="toc-no">{index:02d}</span><span class="toc-text">{title}</span></a>'
                '<button class="toc-toggle" type="button" aria-label="展开或收起小节">'
                '<span class="chev">▾</span></button></div>'
                f'<ul class="toc-h3">{items}</ul></div>'
            )
        else:
            chunks.append(
                f'<div class="toc-group" data-slug="{anchor}">'
                f'<div class="toc-row"><a class="toc-h2" href="#{anchor}">'
                f'<span class="toc-no">•</span><span class="toc-text">{title}</span></a></div></div>'
            )
    return "\n".join(chunks) + '\n    <div class="toc-empty" id="tocEmpty" style="display:none">没有匹配的小节</div>'


def _parse_metadata(markdown_text: str) -> dict[str, str]:
    """读取标题下方的粗体元数据行，供 HTML hero 区域展示。"""

    metadata: dict[str, str] = {}
    for key, value in re.findall(
        r"^\*\*([^*]+)：\*\*\s*(.+)$", markdown_text, flags=re.MULTILINE
    ):
        metadata[key.strip()] = value.strip()
    return metadata


def _render_hero(metadata: dict[str, str]) -> str:
    """根据 Markdown 元数据生成 HTML 顶部说明。"""

    ordered_keys = ("文档状态", "事实截止", "代码基线", "面试方向", "阅读方式", "阅读层次")
    rows = "".join(
        f'<div class="meta-row"><span class="meta-key">{html.escape(key)}</span>'
        f'<span class="meta-val">{html.escape(metadata[key].replace("`", ""))}</span></div>'
        for key in ordered_keys
        if key in metadata
    )
    return (
        '<header class="hero">\n'
        "<h1>高级 RAG 工程面试问答集</h1>\n"
        '<p class="hero__sub">RAG 与工程综合方向 · 初学者可读的 73 题实现解析</p>\n'
        f"{rows}\n"
        '<blockquote class="callout"><p><strong>推荐阅读顺序：</strong>先读术语表，'
        "再看 Q1 的系统总览，然后沿 Q7 入库、Q18 检索、Q25 引用校验理解两条主链路。"
        "每题的白话解析和代码用于理解机制；最后的简版口述回答用于面试准备。</p></blockquote>\n"
        "</header>\n"
    )


def render_document(markdown_path: Path, html_path: Path) -> None:
    """读取现有模板外壳，渲染正文、目录和统计信息。"""

    markdown_text = markdown_path.read_text(encoding="utf-8")
    if not html_path.exists():
        raise FileNotFoundError(f"缺少 HTML 模板：{html_path}")

    current_html = html_path.read_text(encoding="utf-8")
    body_start = current_html.find('    <header class="hero">')
    tail_start = current_html.find('<button class="to-top"')
    if body_start < 0 or tail_start < 0 or tail_start <= body_start:
        raise ValueError("HTML 模板缺少 hero 正文起点或 to-top 终点")

    prefix = current_html[:body_start]
    # 重复生成先替换同名样式，避免每次生成都在页头追加一份。
    prefix = re.sub(r'<style id="teaching-layout">.*?</style>\s*', "", prefix, flags=re.DOTALL)
    prefix = prefix.replace("</head>", TEACHING_STYLES + "\n</head>", 1)
    suffix = current_html[tail_start:]
    nav_start = prefix.find('  <nav class="toc"')
    nav_end = prefix.find("  </nav>", nav_start)
    if nav_start < 0 or nav_end < 0:
        raise ValueError("HTML 模板缺少侧栏 nav")

    headings = [
        (len(match.group(1)), match.group(2).strip())
        for match in re.finditer(
            r"^(#{2,3})\s+(.+)$", markdown_text, flags=re.MULTILINE
        )
    ]
    sidebar = _build_sidebar(headings)
    prefix = prefix[:nav_start] + '  <nav class="toc" aria-label="文章目录">\n' + sidebar + "\n" + prefix[nav_end:]

    chapter_count = sum(1 for level, _ in headings if level == 2)
    section_count = sum(1 for level, _ in headings if level == 3)
    prefix = re.sub(
        r'<span class="topbar__chip">[^<]*</span>',
        f'<span class="topbar__chip">{chapter_count} 章 · {section_count} 节</span>',
        prefix,
        count=1,
    )

    renderer = InterviewHtmlRenderer()
    metadata = _parse_metadata(markdown_text)
    character_count = len(markdown_text)
    footer = (
        '  <footer class="foot">\n'
        "    <p>本页由 <code class=\"inline-code\">advanced-rag-engineering-qa.md</code> "
        "自动生成；每题均包含白话解析、步骤、时序图、极简代码和简版口述回答。</p>\n"
        f"    <p>文档统计：{chapter_count} 个章节 · {section_count} 个小节 · "
        f"约 {character_count:,} 字符 ｜ 更新："
        f"{datetime.now(tz=UTC).date().isoformat()}</p>\n"
        "  </footer>\n"
    )
    content_start = markdown_text.find("## 目录")
    if content_start < 0:
        raise ValueError("Markdown 缺少 ## 目录 标题")
    preamble_start = markdown_text.find("> **给完全不了解代码的读者：**")
    if preamble_start < 0:
        raise ValueError("Markdown 缺少初学者术语表")
    # 保留状态定义、设计声明与代码约定，不能只展示术语表而丢掉事实边界。
    preamble = markdown_text[markdown_text.index("> 这不是"):content_start]
    preamble = re.sub(r"^\*\*阅读层次：\*\*.*\n", "", preamble, flags=re.MULTILINE)
    code_convention = metadata.get("代码阅读约定")
    if code_convention:
        preamble = f"**代码阅读约定：** {code_convention}\n\n" + preamble
    preamble_html = renderer.render(preamble.rstrip())
    body = renderer.render("## 目录" + markdown_text[content_start + len("## 目录"):])
    output = (
        f"{prefix}    {_render_hero(metadata)}\n"
        f"{preamble_html}\n{body}\n{footer}{suffix}"
    )
    html_path.write_text(output, encoding="utf-8")


def main() -> None:
    """命令行入口，默认渲染当前项目的面试问答文档。"""

    parser = argparse.ArgumentParser(description="渲染 RAG 面试问答 Markdown 为单文件 HTML")
    parser.add_argument(
        "--markdown",
        type=Path,
        default=Path("docs/interview/advanced-rag-engineering-qa.md"),
    )
    parser.add_argument(
        "--html",
        type=Path,
        default=Path("docs/interview/advanced-rag-engineering-qa.html"),
    )
    args = parser.parse_args()
    render_document(args.markdown, args.html)
    print(f"rendered {args.markdown} -> {args.html}")


if __name__ == "__main__":
    main()
