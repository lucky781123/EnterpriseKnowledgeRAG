"""最小 FastAPI 应用；本轮只提供健康检查和版本信息。"""

from fastapi import FastAPI

from enterprise_rag.config.settings import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="EnterpriseKnowledgeRAG", version="0.1.0")
    app.state.settings = settings or Settings.from_env()

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        return {"status": "ok", "environment": app.state.settings.app_env}

    @app.get("/api/v1/meta")
    async def meta() -> dict[str, str]:
        return {"name": "EnterpriseKnowledgeRAG", "version": "0.1.0"}

    return app


app = create_app()
