"""Celery 配置工厂；阶段任务在下一阶段注册，不提供伪造的入库 task。"""

from enterprise_rag.infrastructure.settings import InfrastructureSettings


def create_celery_app(settings: InfrastructureSettings | None = None):
    """创建 JSON-only Celery app，不发布消息、不隐式启动 Worker。"""
    resolved = settings or InfrastructureSettings.from_env()
    config = resolved.celery_config()
    # 安装 extra 后才需要真实 Celery；普通 API 与离线旧测试不依赖它。
    from celery import Celery

    app = Celery("enterprise_rag")
    app.conf.update(config)
    return app
