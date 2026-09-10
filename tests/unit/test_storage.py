import pytest

from enterprise_rag.storage.local import LocalObjectStorage


@pytest.mark.asyncio
async def test_local_storage_round_trip_and_path_guard(tmp_path):
    storage = LocalObjectStorage(tmp_path)
    await storage.put("tenant-a/doc-1/raw.md", b"hello", "text/markdown")
    assert await storage.exists("tenant-a/doc-1/raw.md")
    assert await storage.get("tenant-a/doc-1/raw.md") == b"hello"
    await storage.delete("tenant-a/doc-1/raw.md")
    assert not await storage.exists("tenant-a/doc-1/raw.md")
    with pytest.raises(ValueError):
        await storage.put("../../outside", b"blocked")
