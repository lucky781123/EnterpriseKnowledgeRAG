"""Celery CLI 要求导出 app 实例，不能把 FastAPI 风格 factory 当成 app。"""

from enterprise_rag.infrastructure.celery_app import create_celery_app
from enterprise_rag.infrastructure.consumer import register_stage_task
from scripts.ingestion_runtime import worker_runtime

# 这里只构造配置与注册，不在 prefork 前连接 PostgreSQL/HTTP/Redis。
app = create_celery_app()
register_stage_task(app, worker_runtime)
