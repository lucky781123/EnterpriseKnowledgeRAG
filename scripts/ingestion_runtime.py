"""显式 Worker/dispatcher 入口，不加载 .env，不触碰默认 API 进程。"""

import asyncio
import json
from contextlib import asynccontextmanager

from enterprise_rag.infrastructure.celery_app import create_celery_app
from enterprise_rag.infrastructure.consumer import celery_publisher, register_stage_task
from enterprise_rag.ingestion.task_ledger import OutboxDispatcher
from scripts.local_p0_api import create_local_p0_app


@asynccontextmanager
async def worker_runtime():
    """Worker 不加载 Reranker；共享 factory 保证模型/profile/存储配置一致。"""
    application = create_local_p0_app(reranker=object())
    async with application.router.lifespan_context(application):
        worker = application.state.stage_worker
        if worker is None:
            raise RuntimeError("异步基础设施尚未启用")
        yield worker


def app():
    """仅用于 producer 工厂；CLI 使用 scripts.celery_worker:app 实例。"""
    celery = create_celery_app()
    register_stage_task(celery, worker_runtime)
    return celery


async def dispatch(*, once=False):
    """独立恢复进程；维护窗口阻止所有 PG 状态变更与新消息投递。"""
    from enterprise_rag.infrastructure.write_gate import MaintenanceBlocked

    celery = create_celery_app()
    try:
        async with worker_runtime() as worker:
            dispatcher = OutboxDispatcher(worker.ledger, celery_publisher(celery))
            while True:
                try:
                    async with worker.write_gate.writer():
                        expired = await worker.ledger.recover_expired(limit=100)
                        lost = await worker.ledger.recover_deliveries(limit=100)
                        sent = await dispatcher.dispatch_once(limit=100)
                    # 仅输出聚合计数，不含租户、event ID、正文或凭据。
                    print(json.dumps({"expired": expired, "redelivered": lost, "sent": sent}))
                except MaintenanceBlocked:
                    print('{"maintenance": true}')
                if once:
                    break
                await asyncio.sleep(5)
    finally:
        celery.close()


def main():
    """命令行默认持续调度，--once 用于受控 smoke。"""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    asyncio.run(dispatch(once=args.once))


if __name__ == "__main__":
    main()
