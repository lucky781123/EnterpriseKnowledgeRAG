"""Celery JSON-only 阶段消费；broker 只携带 opaque event ID。"""

import asyncio
import re

TASK_NAME = "enterprise_rag.ingestion.stage"


def register_stage_task(app, runtime_factory):
    """注入运行时工厂，避免 prefork 前创建 HTTP/asyncpg 连接池。"""

    @app.task(name=TASK_NAME, ignore_result=True)
    def consume(event_id):
        """每次任务在同一 event loop 内创建和关闭资源，不跨循环复用异步连接。"""
        if not isinstance(event_id, str) or not re.fullmatch(r"event_[0-9a-f]{32}", event_id):
            raise ValueError("event_id 非法")

        async def execute():
            """业务重试写 PG，不调用 Celery retry 重置阶段预算。"""
            async with runtime_factory() as worker:
                return await worker.run_event(event_id)

        return asyncio.run(execute())

    return consume


def celery_publisher(app):
    """producer 只确认 broker 接受；最终消费仍由 PG 恢复器保证可追踪。"""

    async def publish(event_id):
        """在线程执行同步 broker I/O，底层 timeout/retry 限制等待。"""
        await asyncio.to_thread(
            app.send_task, TASK_NAME, args=[event_id], serializer="json",
            retry=False, ignore_result=True,
        )

    return publish
