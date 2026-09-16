# F2-G 复现步骤

本文只使用当前仓库和用户已经配置的本地服务。命令不会打印 `.env`、API key 或完整敏感 DSN；集成测试若使用 `POSTGRES_DSN`，必须确认它是专用开发/CI 数据库，因为 fixture 会创建并删除随机临时数据库。

## 1. 准备环境

```bash
conda activate aitest
cd /Users/liuhui/work/CodexWork/EnterpriseKnowledgeRAG
```

离线契约测试不要求 PostgreSQL。要运行 PostgreSQL 集成测试，可让项目从根目录 `.env` 读取 `POSTGRES_DSN`，或只在当前进程临时导出它；不要把密码写进 shell 历史或文档。

## 2. 离线验证

```bash
conda run -n aitest python -m pytest -q
conda run -n aitest python -m ruff check .
conda run -n aitest python -m compileall -q src tests
git diff --check
```

当前记录的离线结果：`101 passed, 7 skipped`。7 个跳过项是没有向该测试进程提供 `POSTGRES_DSN` 时的 PostgreSQL 集成测试；若需要把它们变为真实证据，必须按下一节使用有效的专用开发 DSN 单独运行。

## 3. PostgreSQL 真实集成（可选）

确认当前 shell 或根目录 `.env` 中的 DSN 指向开发数据库后，运行：

```bash
conda run -n aitest python -m enterprise_rag.db.migration_cli
conda run -n aitest python -m pytest -q tests/integration/test_postgres.py
```

fixture 会在同一个 PostgreSQL 容器中创建随机临时数据库，测试结束后使用 `DROP DATABASE ... WITH (FORCE)` 删除；不会清理固定库的 `public` schema，也不应使用 `docker compose down -v`。

## 4. LM Studio 前置检查

在 LM Studio 中加载：

`qwen3.6-35b-a3b-claude-4.7-opus-reasoning-distilled-vlm-mlx-oq6`

确认服务监听 `http://localhost:1234/v1`。只检查模型列表，不输出密钥：

```bash
conda run -n aitest python -c 'import httpx; print(httpx.get("http://localhost:1234/v1/models", timeout=10).status_code)'
```

Provider adapter 会调用 OpenAI-compatible 的 `/v1/models`、`/v1/chat/completions`；本地 API key 仅作为兼容字段，不写入日志。

## 5. Fake 确定性基线

```bash
conda run -n aitest python scripts/excerpt_benchmark.py \
  --provider fake \
  --iterations 10 \
  --output artifacts/benchmarks/f2-g-fake-2026-09-14.json
```

预期为 10/10 成功。该结果只测应用层和 identity 校验，不代表模型容量、检索质量或真实端到端延迟。

## 6. LM Studio 有界 benchmark

`max_tokens=128` 用于保留截断证据：

```bash
conda run -n aitest python scripts/excerpt_benchmark.py \
  --provider local \
  --base-url http://localhost:1234/v1 \
  --model qwen3.6-35b-a3b-claude-4.7-opus-reasoning-distilled-vlm-mlx-oq6 \
  --iterations 10 \
  --max-tokens 128 \
  --concurrency 1 2 \
  --warmup 1 \
  --cancel-runs 2 \
  --group-timeout-seconds 600 \
  --output artifacts/experiments/2026-09-14_f2-g/local-benchmark-128.json
```

`max_tokens=256` 用于观察完整终态：

```bash
conda run -n aitest python scripts/excerpt_benchmark.py \
  --provider local \
  --base-url http://localhost:1234/v1 \
  --model qwen3.6-35b-a3b-claude-4.7-opus-reasoning-distilled-vlm-mlx-oq6 \
  --iterations 10 \
  --max-tokens 256 \
  --concurrency 1 2 \
  --warmup 1 \
  --cancel-runs 2 \
  --group-timeout-seconds 600 \
  --output artifacts/experiments/2026-09-14_f2-g/local-benchmark-256.json
```

每个产物都记录代码 commit、dirty patch hash、fixture/Prompt/config hash、运行时版本、模型发现结果和 warnings。正常样本与主动取消样本分开统计；错误、截断和取消不会计入成功延迟分位数。

## 7. 结果判读

- `max_tokens=128` 若全部出现 `finish_reason=length`，应报告为模型调用成功但应用回答不完整，而不是把 HTTP 200 当作业务成功。
- `max_tokens=256` 的成功只表示应用层终态完整且摘录身份可核验；它不等于事实支持、检索召回或人工质量通过。
- JSON usage 可见而 SSE usage 缺失时，不要从字符数或时间反推 token 吞吐。
- `completion_ms` 包含排队；SSE TTFT 只表示首个可见正文增量。
- cleanup 时延表示本地资源回收，不证明上游 GPU 停止生成。
- 真实结果只能用于当前机器和配置的工程观察，不可直接写成生产容量、SLO 或成本结论。

## 8. 产物位置

- 评测说明：`docs/evaluation/f2-g-evaluation.md`
- 实验配置：`artifacts/experiments/2026-09-14_f2-g/config.json`
- 汇总指标：`artifacts/experiments/2026-09-14_f2-g/metrics.json`
- 截断/异常记录：`artifacts/experiments/2026-09-14_f2-g/failures.jsonl`
- 实验结论：`artifacts/experiments/2026-09-14_f2-g/conclusion.md`
