"""公开在线演练专用 Celery Worker，不修改业务身份目录。"""

import os
from contextlib import asynccontextmanager

from sqlalchemy.engine import make_url

from enterprise_rag.infrastructure.celery_app import create_celery_app
from enterprise_rag.infrastructure.consumer import register_stage_task
from enterprise_rag.security.authorization import ResourceNotFound
from enterprise_rag.security.context import RequestContext
from scripts.deepseek_smoke import TEST_TENANT
from scripts.local_p0_api import create_local_p0_app


async def public_identity(tenant, actor):
    """仅授权固定合成测试身份；其他消息一律拒绝，不沿用消息角色快照。"""
    import time

    if tenant != TEST_TENANT or actor != "public-smoke-user":
        raise ResourceNotFound("任务身份不可见")
    now = int(time.time())
    return RequestContext(
        tenant_id=tenant, actor_id=actor, roles=frozenset({"reader", "editor"}),
        issued_at=now, expires_at=now + 60, token_version=1,
    )


@asynccontextmanager
async def public_runtime():
    """创建与主 smoke 相同的真实服务装配，仅替换测试身份 resolver。"""
    # 专用 Worker 也必须独立验证目标，不能仅相信启动它的父进程已检查隔离。
    url = make_url(os.environ["POSTGRES_DSN"])
    if (
        os.getenv("INFRA_SMOKE_ISOLATED") != "true"
        or url.host not in {"127.0.0.1", "localhost"}
        or not (url.database or "").startswith("infra_test")
    ):
        raise RuntimeError("公开 Worker 必须使用专用隔离数据库")
    application = create_local_p0_app(reranker=object(), identity_resolver=public_identity)
    async with application.router.lifespan_context(application):
        if application.state.stage_worker is None:
            raise RuntimeError("公开异步演练尚未启用")
        yield application.state.stage_worker


app = create_celery_app()
register_stage_task(app, public_runtime)
