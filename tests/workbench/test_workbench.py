"""F2-I 工作台依赖和调用边界测试。"""

import pytest

from enterprise_rag.workbench.app import build_workbench


def test_workbench_reports_optional_dependency_boundary() -> None:
    """未安装 Gradio 时应给出可操作的明确错误，而不是静默失败。"""

    try:
        import gradio  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match="gradio"):
            build_workbench()
