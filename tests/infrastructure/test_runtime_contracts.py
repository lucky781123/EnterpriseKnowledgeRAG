"""Worker 注册、身份重载和门禁生命周期的离线契约。"""

from contextlib import asynccontextmanager

import pytest

from enterprise_rag.db.session import Database
from enterprise_rag.infrastructure.consumer import TASK_NAME, register_stage_task
from enterprise_rag.infrastructure.write_gate import MaintenanceBlocked, WriteGate
from enterprise_rag.security.authorization import ResourceNotFound
from enterprise_rag.security.worker_identity import FileIdentityResolver


class TaskApp:
    """仅模拟 Celery 装饰器，不模拟真实 broker。"""

    def task(self, **kwargs):
        """验证注册遵守固定 opaque 参数和不存储结果正文。"""
        assert kwargs == {"name": TASK_NAME, "ignore_result": True}
        return lambda function: function


def test_consumer_registers_opaque_event_only():
    """同步 Celery task 每次拥有独立异步生命周期。"""
    seen = []

    class Worker:
        """记录服务器事件 ID。"""

        async def run_event(self, event):
            """不接受 tenant/profile 或原文参数。"""
            seen.append(event)
            return "completed"

    @asynccontextmanager
    async def runtime():
        """资源由本次 event loop 管理，结束必须清理。"""
        try:
            yield Worker()
        finally:
            seen.append("closed")

    task = register_stage_task(TaskApp(), runtime)
    event = "event_" + "1" * 32
    assert task(event) == "completed"
    assert seen == [event, "closed"]
    with pytest.raises(ValueError):
        task({"tenant": "untrusted"})


async def test_identity_directory_reloads_revocation(tmp_path):
    """角色文件变更后后续阶段立即 fail closed。"""
    path = tmp_path / "identities.toml"
    path.write_text(
        '[[members]]\ntenant_id="a"\nactor_id="alice"\nroles=["editor"]\nactive=true\n',
    )
    resolver = FileIdentityResolver(path)
    assert (await resolver("a", "alice")).has_role("editor")
    path.write_text(
        '[[members]]\ntenant_id="a"\nactor_id="alice"\nroles=["editor"]\nactive=false\n',
    )
    with pytest.raises(ResourceNotFound):
        await resolver("a", "alice")
    with pytest.raises(ResourceNotFound):
        await resolver("b", "alice")


async def test_write_gate_rejects_sqlite_instead_of_faking_pg_lock(tmp_path):
    """SQLite 不支持生产维护门禁，不把无操作模拟成锁成功。"""
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'gate.db'}")
    try:
        with pytest.raises(MaintenanceBlocked):
            async with WriteGate(database).writer():
                pytest.fail("不应进入业务区间")
    finally:
        await database.dispose()
