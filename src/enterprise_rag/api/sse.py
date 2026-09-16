"""F2-G SSE 事件格式化工具。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def format_sse_event(event: Mapping[str, Any]) -> str:
    """将单个业务事件编码为 data-only SSE 帧，避免输出内部推理或原始异常。"""

    return f"data: {json.dumps(dict(event), ensure_ascii=False, separators=(',', ':'))}\n\n"
