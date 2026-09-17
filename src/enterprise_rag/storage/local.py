"""本地文件对象存储：开发和测试默认实现。"""

from __future__ import annotations

import asyncio
from pathlib import Path


class LocalObjectStorage:
    """本地对象存储实现，并提供只读 readiness 探针。"""

    def __init__(self, root: str | Path) -> None:
        """保存解析后的根目录；所有 key 都会经过路径边界检查。"""

        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        candidate = (self.root / key).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError("对象 key 不能跳出存储根目录")
        return candidate

    async def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        del content_type  # 本地实现不保存 MIME；接口保留该参数供 S3 适配器使用。
        path = self._path(key)
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, data)

    async def get(self, key: str) -> bytes:
        return await asyncio.to_thread(self._path(key).read_bytes)

    async def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            await asyncio.to_thread(path.unlink)

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._path(key).exists)

    async def health_check(self) -> bool:
        """只读检查对象存储根目录存在且为目录。"""

        return await asyncio.to_thread(self.root.is_dir)
