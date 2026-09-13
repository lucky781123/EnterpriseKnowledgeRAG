"""最小 FastAPI 应用；本轮提供存活、数据库 schema readiness 和版本信息。"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Response

from enterprise_rag.config.settings import Settings
from enterprise_rag.db.session import Database


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or Settings.from_env()
    database = None
    if resolved_settings.postgres_dsn:
        database = Database(
            resolved_settings.postgres_dsn,
            pool_size=resolved_settings.postgres_pool_size,
            max_overflow=resolved_settings.postgres_max_overflow,
            pool_timeout=resolved_settings.postgres_pool_timeout_seconds,
            pool_recycle=resolved_settings.postgres_pool_recycle_seconds,
            statement_timeout_ms=resolved_settings.postgres_statement_timeout_ms,
            lock_timeout_ms=resolved_settings.postgres_lock_timeout_ms,
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        """应用关闭时释放连接池；启动阶段不执行 migration。"""

        yield
        if database is not None:
            await database.dispose()

    app = FastAPI(title="EnterpriseKnowledgeRAG", version="0.1.0", lifespan=lifespan)
    app.state.settings = resolved_settings
    app.state.database = database

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready(response: Response) -> dict[str, object]:
        """只有数据库 schema 已达到目标版本时，服务才进入 ready。"""

        if app.state.database is None:
            response.status_code = 503
            return {
                "status": "not_ready",
                "environment": app.state.settings.app_env,
                "database": {"status": "not_configured"},
            }

        readiness = await app.state.database.schema_readiness()
        if not readiness.ready:
            response.status_code = 503
            return {
                "status": "not_ready",
                "environment": app.state.settings.app_env,
                "database": {"status": readiness.status},
            }

        return {
            "status": "ok",
            "environment": app.state.settings.app_env,
            "database": {
                "status": readiness.status,
                "migration": readiness.target_version,
            },
        }

    @app.get("/api/v1/meta")
    async def meta() -> dict[str, str]:
        return {"name": "EnterpriseKnowledgeRAG", "version": "0.1.0"}

    return app


app = create_app()
