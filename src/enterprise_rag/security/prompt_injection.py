"""RAG evidence 的 Prompt Injection 检测和不可信边界。

检测器只决定是否需要阻断或人工复核，不把检测结果当成完整安全证明。
文档内容始终作为 data 注入 Prompt，不能获得 system/tool 权限。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class InjectionFinding:
    """一条脱敏检测结果，不保存命中的原文。"""

    rule: str
    severity: str


class PromptInjectionDetected(ValueError):
    """证据包含高风险指令，不能直接送入回答模型。"""

    def __init__(self, findings: Sequence[InjectionFinding]) -> None:
        """只保留规则和严重级别，避免异常携带原文。"""

        self.findings = tuple(findings)
        super().__init__("evidence 包含高风险 Prompt Injection 指令")


_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "override_instructions",
        "high",
        re.compile(
            r"(ignore|disregard|override)\s+(all\s+|the\s+)?(previous|prior|system|developer)"
            r"|(忽略|无视|覆盖).{0,12}(之前|系统|开发者|指令)",
            re.IGNORECASE,
        ),
    ),
    (
        "secret_exfiltration",
        "high",
        re.compile(
            r"(reveal|print|send|export).{0,24}(secret|api[\s_-]?key|token|password)"
            r"|(泄露|输出|发送|导出).{0,20}(密钥|令牌|密码|token)",
            re.IGNORECASE,
        ),
    ),
    (
        "tool_or_shell_execution",
        "high",
        re.compile(
            r"(run|execute|call).{0,20}(shell|bash|python|sql|tool)"
            r"|(执行|调用).{0,20}(命令|shell|SQL|工具)",
            re.IGNORECASE,
        ),
    ),
)


def scan_evidence(texts: Sequence[str]) -> tuple[InjectionFinding, ...]:
    """扫描 evidence 文本并只返回规则摘要，不返回匹配文本。"""

    findings: list[InjectionFinding] = []
    seen: set[str] = set()
    for text in texts:
        for rule, severity, pattern in _RULES:
            if pattern.search(text) and rule not in seen:
                findings.append(InjectionFinding(rule, severity))
                seen.add(rule)
    return tuple(findings)


def enforce_evidence_boundary(texts: Sequence[str]) -> None:
    """阻断高风险指令证据，避免其进入模型上下文。"""

    findings = scan_evidence(texts)
    if any(item.severity == "high" for item in findings):
        raise PromptInjectionDetected(findings)


__all__ = [
    "InjectionFinding",
    "PromptInjectionDetected",
    "enforce_evidence_boundary",
    "scan_evidence",
]
