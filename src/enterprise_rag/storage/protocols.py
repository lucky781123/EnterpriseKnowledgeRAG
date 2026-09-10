from __future__ import annotations

from typing import Protocol


class ObjectStorage(Protocol):
    async def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        ...

    async def get(self, key: str) -> bytes:
        ...

    async def delete(self, key: str) -> None:
        ...

    async def exists(self, key: str) -> bool:
        ...
