"""开发身份目录；每阶段重读服务端文件，生产须注入真实成员关系服务。"""

import time
import tomllib
from pathlib import Path

from enterprise_rag.security.authorization import ResourceNotFound
from enterprise_rag.security.context import RequestContext


class FileIdentityResolver:
    """固定项目文件维护测试成员关系，不相信任务保存的角色快照。"""

    def __init__(self, path: Path):
        """只保存服务器配置的目录位置，不允许消息指定此路径。"""
        self.path = path

    async def __call__(self, tenant: str, actor: str):
        """每次重新验证目录中的 active/roles，撤权无需重启 Worker。"""
        import asyncio

        def resolve():
            """文件缺失或身份歧义时拒绝，不能回退到任务快照。"""
            try:
                with self.path.open("rb") as source:
                    records = tomllib.load(source)["members"]
                matches = [
                    record for record in records
                    if record["tenant_id"] == tenant and record["actor_id"] == actor
                ]
                if len(matches) != 1 or matches[0].get("active") is not True:
                    raise ValueError()
                now = int(time.time())
                return RequestContext(
                    tenant_id=tenant, actor_id=actor, roles=frozenset(matches[0]["roles"]),
                    issued_at=now, expires_at=now + 60, token_version=1,
                )
            except (OSError, KeyError, TypeError, ValueError):
                raise ResourceNotFound("任务身份不可见") from None

        return await asyncio.to_thread(resolve)
