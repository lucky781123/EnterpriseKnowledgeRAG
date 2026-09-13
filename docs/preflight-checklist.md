# 开工前置检查清单

更新时间：2026-09-12

## A. 已确认

- [x] Mac Apple Silicon：`arm64`。
- [x] M3 Max，128 GB 统一内存，2 TB SSD。
- [x] 磁盘可用空间约 221 GiB，必要时可继续清理。
- [x] Docker CLI 和 Docker Compose 命令存在。
- [x] `kubectl` 命令存在。
- [x] `aitest` Conda 环境存在，Python 3.11.15 可运行。
- [x] Ollama Embedding 已由用户验证：`hardcore_aryabhata_598/qwen3-embedding:8b` 可正常运行。
- [x] LM Studio 本地 LLM API 已由用户验证可问答；首期模型已冻结为 `qwen3.6-35b-a3b-claude-4.7-opus-reasoning-distilled-vlm-mlx-oq6`。
- [x] Reranker 改为工程内 `BGEReranker`；通过统一 Adapter 提供异步接口，已完成 Fake 契约测试。
- [x] 在线 Grok 配置已明确为环境变量、第三方 OpenAI-compatible base URL 和 `grok-4.6`。
- [x] 评测集草案已生成：40 条，JSONL 格式有效。
- [x] PostgreSQL `16.15` 开发容器已验证：容器名 `enterprise-rag-postgres`，本机端口 `54329`，镜像固定为 `postgres:16`。
- [x] PostgreSQL 初始 migration、约束、事务回滚和并发发布已在真实数据库验证。
- [x] migration 已有 one-shot CLI/Compose init job；API readiness 只检查 schema/migration 版本，不自动迁移。
- [x] 本地 Compose 默认复用 `enterprise-rag-postgres` 的 `54329` 端口，避免重复创建同名容器；数据库密码通过 `POSTGRES_DSN` 显式提供。

## B. 必须在第一轮基础设施集成前完成

- [x] 用户已在本机终端验证 Docker Desktop 正常：Docker 29.5.2，Server `linux/aarch64`，16 CPU，15.6 GiB daemon memory，12 个容器运行中。
- [ ] 对 Redis、RabbitMQ、Weaviate、Keycloak 镜像做 `linux/arm64` 可用性检查；PostgreSQL 已完成本轮固定镜像验证。
- [ ] 确定首期对象存储实现；代码先使用本地适配器，避免阻塞。
- [ ] 验证 Ollama Embedding：维度、批量、超长输入、超时和错误。
- [ ] 验证工程内 BGE Reranker：模型加载、批量大小、MPS 内存、超时和线程池并发。
- [x] 选择首期 LM Studio 默认模型名；非流式/流式/JSON 输出契约仍待真实服务联调。
- [ ] 确认 `GROK_API_KEY` 只存在于本地环境变量；在线调用关闭前不进行真实评测。

## C. 可在实现过程中完成

- [ ] Minikube 安装和单节点部署验证。
- [ ] `omlx` 小模型运行时对照基准。
- [ ] 受控 PDF 样本解析。
- [ ] 评测集人工抽样确认和分层冻结。
- [ ] Agentic 路径。
- [ ] 在线模型质量抽样和低并发延迟抽样。
- [ ] Minikube 多节点和故障演练。

## D. 当前环境异常

2026-09-09 初次检查时 Codex 沙箱无法连接 Docker socket；随后用户在本机终端验证 Docker Desktop 已正常运行，Server 为 `linux/aarch64`。Codex 沙箱当前仍可能因 socket 权限无法复跑 Docker API，因此“用户已验证”和“本会话沙箱已复核”需要区分。主机与 Docker daemon 均为 ARM 架构，镜像仍需逐个检查 `linux/arm64` manifest。

`minikube` 当前未安装；`kubectl` 已安装。Minikube 不阻塞第一轮 API、数据模型和模型契约开发，但会阻塞 Kubernetes 验收轮次。

## E. 架构检查命令

Docker daemon 恢复后执行：

```bash
docker info --format 'OS={{.OSType}} ARCH={{.Architecture}}'
docker buildx inspect --bootstrap
docker buildx imagetools inspect postgres:16
docker buildx imagetools inspect redis:7
docker buildx imagetools inspect rabbitmq:3-management
docker buildx imagetools inspect semitechnologies/weaviate:latest
```

要求优先选择带 `linux/arm64` manifest 的固定版本镜像；不要用 `latest` 作为最终锁定版本。

## F. 判定

当前可以开始：PostgreSQL 连接可靠性收口、真实摄取基础和后续 Provider 契约开发。

当前不能宣称完成：生产级连接容量/故障转移/备份恢复、Redis/RabbitMQ/Weaviate/Keycloak 集成、Minikube 部署和端到端模型链路。
