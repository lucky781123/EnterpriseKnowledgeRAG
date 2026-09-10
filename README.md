# EnterpriseKnowledgeRAG

面向研发技术文档的企业级 RAG 学习与验证项目。

> 当前状态：`Phase 2` 基础工程骨架。当前版本可运行健康检查、Provider/Fake LLM、配置校验和基础单元测试；PostgreSQL、Weaviate、权限、摄取、检索和问答闭环仍在后续阶段实施。

当前目标是完成：

- P0：可运行的端到端闭环；
- P1：检索、权限、可靠性、性能有可复现实验证据；
- P2：清晰、诚实、带触发条件的生产演进方案。

## 当前开发策略

- 本地 LLM：LM Studio，默认模型为 `qwen3.6-35b-a3b-claude-4.7-opus-reasoning-distilled-vlm-mlx-oq6`。
- Embedding：Ollama，已验证 `hardcore_aryabhata_598/qwen3-embedding:8b` 可运行。
- Reranker：进程内 `BGEReranker + SyncRerankerAdapter`。
- 在线 LLM：通过 OpenAI-compatible Provider 接入，默认关闭，仅用于抽样评测。
- `omlx`：后续小模型运行时实验，不是主链路前置依赖。

## 第 1 轮当前范围

本轮先完成不依赖外部服务的：

- 配置加载与校验；
- Provider 抽象和 Fake LLM；
- 评测集加载器；
- 领域数据结构初稿；
- FastAPI 健康检查；
- 单元测试基础设施。

详细状态见 [PROJECT_CONTEXT.md](./PROJECT_CONTEXT.md) 和 [docs/preflight-checklist.md](./docs/preflight-checklist.md)。

## 快速开始

### 1. 创建 Python 环境

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

如果要运行本地 BGE Reranker，额外安装：

```bash
python -m pip install -e '.[reranker]'
```

### 2. 配置环境变量

```bash
cp .env.example .env
set -a
source .env
set +a
```

当前 `Settings` 读取进程环境变量，不会自动加载 `.env` 文件。真实服务联调时请使用上面的 shell 导出方式或其他环境变量管理工具；不要提交真实密钥。

### 3. 运行测试和检查

```bash
python -m pytest -q
python -m ruff check .
python -m compileall -q src tests
```

### 4. 启动当前 API 骨架

```bash
uvicorn enterprise_rag.api.app:app --reload
```

```bash
curl http://127.0.0.1:8000/health/live
curl http://127.0.0.1:8000/health/ready
curl http://127.0.0.1:8000/api/v1/meta
```

### 5. 使用 Docker Compose

```bash
docker compose up --build
```

当前 Compose 只启动 API 骨架并提供健康检查，不会伪装成已经接通 PostgreSQL、Redis、RabbitMQ、Weaviate 或 Keycloak。真实基础设施会在对应实现阶段加入。

## 配置文件

- `.env.example`：公开的开发环境变量模板。
- `config/model-registry.example.yaml`：公开的模型能力登记表模板。
- `config/model-registry.yaml`：当前本地验证基线，不含密钥。
- `config/model-registry.local.yaml`：本地覆盖文件，不应提交。

`.planning/` 是本地开发工作区，默认不纳入发布仓库；需要公开开发记录时，请整理到 `docs/` 后再提交。

## 项目结构

```text
src/enterprise_rag/   Python 包和领域代码
tests/                单元、API、契约和集成测试
docs/                 架构、运维、评测和 ADR
config/               非敏感配置模板
scripts/              本地验证脚本
Dockerfile            API 镜像构建文件
compose.yaml          当前 API 骨架的 Compose 配置
```

## 文档导航

文档入口见 [docs/README.md](./docs/README.md)。评测集目前仍是草案，引用的企业文档样本尚未全部纳入仓库，因此不能直接视为完整 RAG 评测基线。

## 当前限制

- 首期不承诺复杂扫描 PDF、复杂表格和版面完全还原。
- Fake Provider、本地模型和单机 Compose 不能代表生产容量或企业真实效果。
- 当前版本没有完成数据库迁移、文档摄取、检索、ACL、SSE 闭环。

## 安全

请阅读 [SECURITY.md](./SECURITY.md)。任何 API Key、密码、token、数据库连接串都只能通过本地环境变量或外部密钥管理系统提供。

## 贡献

开发流程见 [CONTRIBUTING.md](./CONTRIBUTING.md)。

## License

本项目使用 [MIT License](./LICENSE)。
