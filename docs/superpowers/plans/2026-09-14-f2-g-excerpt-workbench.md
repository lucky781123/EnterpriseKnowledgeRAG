# F2-G 原文摘录问答与精简工作台实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans（本计划在当前会话按任务逐项执行；每个任务均先测试后实现）。

**Goal:** 在调用方提交 `question + evidence` 的 F-2 边界内，构建可核验的原文摘录问答 API，并提供调用同一 API 的本地精简 Gradio 工作台。

**Architecture:** FastAPI route 只负责 HTTP/Pydantic/SSE；`ExcerptAnswerService` 负责证据标签、Prompt、摘录解析与原文位置核验；`ChatProvider`/LM Studio adapter 负责模型传输和错误归一化；工作台只调用 API，不复制生成逻辑。F2-G 先完成并可独立测试，F2-I 作为后续薄 UI 接入。

**Tech Stack:** Python 3.11、FastAPI、Pydantic v2、HTTPX async、现有 Provider protocol/Fake/scheduler、Gradio（仅本地调试依赖，若未安装则单独补充开发依赖）。

**Spec:** `docs/superpowers/specs/2026-09-14-f2-evidence-provider-design.md`

## Global Constraints

- 只接受调用方提交的 `question + evidence`；`context_source` 固定为 `caller_supplied`。
- 不读取数据库、对象存储、Weaviate，不实现真实摄取、检索、ACL/OIDC、trace replay 或在线 fallback。
- 摘录必须满足 `evidence.text[start:end] == quote`；程序不改写模型摘录、不伪造引用。
- 已确认的项目 SSE 事件集合只使用 `stage/tool_call/token/citation/error/done`；F2-G 不产生 `tool_call`。
- Provider 错误必须归一化；不向外暴露密钥、完整 DSN、Authorization header、原始敏感 body 或内部推理。
- 所有新增代码和测试添加详细中文注释，解释契约、信任边界、边界条件和取消清理。
- 不把摘录 identity 校验描述为事实正确性、权限验证或完整 RAG 证据。
- 每个任务完成后运行其定向测试；阶段末运行完整离线回归与静态检查。

### Task 1: 固定原文摘录领域契约与确定性校验

**Files:**
- Create: `src/enterprise_rag/excerpts/protocols.py`
- Create: `src/enterprise_rag/excerpts/validation.py`
- Create: `src/enterprise_rag/excerpts/__init__.py`
- Test: `tests/unit/test_excerpt_validation.py`

**Interfaces:**
- `EvidenceInput(evidence_id: str, text: str, source: str | None = None)`
- `ExcerptCandidate(evidence_id: str, quote: str, start: int | None = None, end: int | None = None)`
- `ValidatedExcerpt(evidence_id: str, quote: str, start: int, end: int, source: str | None)`
- `validate_evidence_bundle(evidence: Sequence[EvidenceInput]) -> tuple[EvidenceInput, ...]`
- `validate_excerpt(candidate: ExcerptCandidate, evidence: Mapping[str, EvidenceInput]) -> ValidatedExcerpt`

- [ ] **Step 1: Write failing tests**
  - 覆盖空 evidence、重复 ID、非法 ID、空正文、未知 evidence ID、quote 与原文不相等、缺少位置、位置越界、重复文本时指定位置、Unicode/换行位置。
- [ ] **Step 2: Run tests**
  - Run: `pytest tests/unit/test_excerpt_validation.py -q`
  - Expected: FAIL because the module and validation functions do not exist.
- [ ] **Step 3: Implement minimal domain types and validators**
  - 使用不可变 dataclass；位置采用 Python 字符索引；若不传位置则仅允许 quote 在原文中唯一出现，否则拒绝歧义。
  - 在复杂分支前写中文注释，说明“identity 校验不等于语义支持”。
- [ ] **Step 4: Run tests**
  - Run: `pytest tests/unit/test_excerpt_validation.py -q`
  - Expected: PASS.
- [ ] **Step 5: Commit**
  - Run: `git add src/enterprise_rag/excerpts tests/unit/test_excerpt_validation.py && git commit -m "feat: add excerpt identity validation contract"`

### Task 2: Provider 协议扩展、LM Studio adapter 与错误归一化

**Files:**
- Modify: `src/enterprise_rag/providers/protocols.py`
- Modify: `src/enterprise_rag/providers/errors.py`
- Create: `src/enterprise_rag/providers/lmstudio.py`
- Create: `src/enterprise_rag/providers/registry.py`
- Test: `tests/contract/test_lmstudio_provider.py`
- Test: `tests/unit/test_provider_errors.py`

**Interfaces:**
- `ChatUsage(input_tokens: int | None, output_tokens: int | None)`
- `ChatResponse(content: str, model: str, finish_reason: str | None, usage: ChatUsage | None)`
- `LMStudioChatProvider(client: httpx.AsyncClient, base_url: str, model: str, api_key: str | None = None)`
- `ProviderRegistry.resolve(alias: str, capability: str) -> RegisteredProvider`
- `LMStudioChatProvider.complete(request: ChatRequest) -> ChatResponse`
- `LMStudioChatProvider.stream(request: ChatRequest) -> AsyncIterator[str]`
- `LMStudioChatProvider.health_check() -> bool`

- [ ] **Step 1: Write failing mock HTTP tests**
  - 覆盖 `/v1/chat/completions` URL 拼接、字段映射、usage 缺失、finish_reason、stream SSE data、`[DONE]`、碎片 JSON、401/429/5xx、非法 JSON、超时及 API key 不进入异常文本。
- [ ] **Step 2: Run tests**
  - Run: `pytest tests/contract/test_lmstudio_provider.py tests/unit/test_provider_errors.py -q`
  - Expected: FAIL because adapter and new response/error fields are absent.
- [ ] **Step 3: Implement adapter and registry**
  - 共享 `httpx.AsyncClient`；adapter 只做传输、解析和错误分类；不实现 Prompt、引用或 fallback。
  - 增加 `ProviderErrorKind.CANCELLED` 与安全消息映射，保持共享 Schema 语义。
  - 写详细中文注释解释超时不是总 deadline、流结束必须看 `[DONE]`。
- [ ] **Step 4: Run tests**
  - Run same command; Expected: PASS.
- [ ] **Step 5: Commit**
  - Run: `git add src/enterprise_rag/providers tests/contract/test_lmstudio_provider.py tests/unit/test_provider_errors.py && git commit -m "feat: add lmstudio chat provider adapter"`

### Task 3: ExcerptAnswerService、结构化输出与完整 JSON API

**Files:**
- Create: `src/enterprise_rag/excerpts/service.py`
- Create: `src/enterprise_rag/api/schemas.py`
- Modify: `src/enterprise_rag/api/app.py`
- Modify: `src/enterprise_rag/config/settings.py`
- Test: `tests/unit/test_excerpt_service.py`
- Test: `tests/api/test_query_api.py`

**Interfaces:**
- `ExcerptQueryRequest`
- `ExcerptResponse`
- `ExcerptAnswerService.answer(request: ExcerptQueryRequest) -> ExcerptResponse`
- `build_excerpt_prompt(question: str, evidence: Sequence[EvidenceInput]) -> tuple[ChatMessage, ...]`
- `create_app(..., provider_registry: ProviderRegistry | None = None) -> FastAPI`

- [ ] **Step 1: Write failing service/API tests**
  - 覆盖正常摘录、重复文本位置、无答案、未知摘录、截断、不合法请求、额外敏感字段、Provider 错误 HTTP 映射和 `context_source=caller_supplied`。
- [ ] **Step 2: Run tests**
  - Run: `pytest tests/unit/test_excerpt_service.py tests/api/test_query_api.py -q`
  - Expected: FAIL because schemas, service and routes are absent.
- [ ] **Step 3: Implement service and JSON route**
  - 使用内部标签 `E1..En`，模型只需返回结构化 JSON：`evidence_id`、`quote`、`start`、`end`、`answer_status`、`message`。
  - 程序核验每个摘录后才返回 `citations`；不返回未经核验的 quote 作为有效引用。
  - 对无法回答返回 `answer_status=not_found`，与模型或传输失败分开。
  - 详细中文注释说明调用方 evidence 是不可信数据，不能扩大权限。
- [ ] **Step 4: Run tests**
  - Run same command; Expected: PASS.
- [ ] **Step 5: Commit**
  - Run: `git add src/enterprise_rag/excerpts src/enterprise_rag/api src/enterprise_rag/config tests/unit/test_excerpt_service.py tests/api/test_query_api.py && git commit -m "feat: add excerpt answer json api"`

### Task 4: SSE 摘录交付与取消清理

**Files:**
- Modify: `src/enterprise_rag/excerpts/service.py`
- Modify: `src/enterprise_rag/api/app.py`
- Create: `src/enterprise_rag/api/sse.py`
- Test: `tests/api/test_query_stream.py`

**Interfaces:**
- `ExcerptAnswerService.stream(request: ExcerptQueryRequest) -> AsyncIterator[dict[str, object]]`
- `format_sse_event(event: Mapping[str, object]) -> str`

- [ ] **Step 1: Write failing SSE tests**
  - 覆盖正常 `stage -> token? -> citation -> done`、引用失败 error/done、malformed upstream、客户端取消后的资源释放和单调 seq。
- [ ] **Step 2: Run tests**
  - Run: `pytest tests/api/test_query_stream.py -q`
  - Expected: FAIL because stream route and formatter are absent.
- [ ] **Step 3: Implement bounded stream**
  - 流式输出原文摘录结果，不透传 reasoning；保持至多一个终态 done，客户端断开不再发事件。
  - 采用剩余 deadline 控制上游与发送，不为发送终态重置预算。
- [ ] **Step 4: Run tests**
  - Run same command; Expected: PASS.
- [ ] **Step 5: Commit**
  - Run: `git add src/enterprise_rag/excerpts src/enterprise_rag/api tests/api/test_query_stream.py && git commit -m "feat: add excerpt answer sse"`

### Task 5: 精简 Gradio 工作台

**Files:**
- Create: `src/enterprise_rag/workbench/app.py`
- Create: `src/enterprise_rag/workbench/__init__.py`
- Modify: `pyproject.toml`
- Modify: `src/enterprise_rag/api/app.py`
- Test: `tests/workbench/test_workbench.py`

**Interfaces:**
- `build_workbench(api_client: httpx.AsyncClient | None = None) -> gradio.Blocks`
- `mount_workbench(app: FastAPI) -> None`

- [ ] **Step 1: Write failing UI contract tests**
  - 覆盖工作台构建、输入 evidence JSON、调用同一 `/api/v1/query`、显示摘录/位置/校验状态、错误不泄露敏感字段。
- [ ] **Step 2: Run tests**
  - Run: `pytest tests/workbench/test_workbench.py -q`
  - Expected: FAIL because Gradio workbench is absent.
- [ ] **Step 3: Implement minimal UI**
  - 仅提供 question、evidence JSON、submit、clear、结果显示和诊断字段；不保存正文、不实现上传/历史/用户管理。
  - 若 Gradio 不在默认 dev 依赖，增加窄版本范围并验证 FastAPI mount 兼容性。
- [ ] **Step 4: Run tests**
  - Run same command; Expected: PASS.
- [ ] **Step 5: Commit**
  - Run: `git add src/enterprise_rag/workbench pyproject.toml src/enterprise_rag/api tests/workbench && git commit -m "feat: add minimal excerpt workbench"`

### Task 6: 评测、benchmark 与交付文档

**Files:**
- Create: `scripts/excerpt_benchmark.py`
- Create: `docs/adr/ADR-010-excerpt-answer-boundary.md`
- Create: `docs/evaluation/f2-g-evaluation.md`
- Create: `docs/operations/f2-g-reproduction.md`
- Create: `artifacts/experiments/2026-09-14_f2-g/README.md`
- Create: `artifacts/experiments/2026-09-14_f2-g/config.json`
- Create: `artifacts/experiments/2026-09-14_f2-g/metrics.json`
- Create: `artifacts/experiments/2026-09-14_f2-g/failures.jsonl`
- Create: `artifacts/experiments/2026-09-14_f2-g/conclusion.md`
- Modify: `config/model-registry.yaml`
- Modify: `PROJECT_CONTEXT.md`
- Modify: `.planning/.../task_plan.md`
- Modify: `.planning/.../findings.md`
- Modify: `.planning/.../progress.md`

- [ ] **Step 1: Write deterministic evaluator tests**
  - 覆盖原文相等、位置正确、未知 evidence、重复文本、not_found、协议失败；输出 `EvaluationObservation` 时未运行的语义指标标记 `not_run`。
- [ ] **Step 2: Run tests**
  - Run: `pytest tests/evaluation/test_excerpt_evaluation.py -q`
  - Expected: FAIL because evaluator and runner are absent.
- [ ] **Step 3: Implement evaluator/benchmark and docs**
  - benchmark 分离 Fake 与真实 LM Studio；记录代码、Prompt、数据和配置 hash；不生成虚假的质量分数或生产容量结论。
- [ ] **Step 4: Run verification**
  - Run: `pytest -q`; `ruff check .`; `python -m compileall -q src tests`; `git diff --check`.
  - Expected: all commands exit 0；真实 LM Studio 不可用时产物写明环境缺口。
- [ ] **Step 5: Commit**
  - Run: `git add scripts docs/adr docs/evaluation docs/operations artifacts/experiments config/model-registry.yaml PROJECT_CONTEXT.md .planning/... && git commit -m "docs: deliver f2-g evidence and benchmark artifacts"`

## Plan Self-Review

- Spec coverage：任务 1 覆盖摘录/位置/输入边界；任务 2 覆盖真实 Provider 与错误；任务 3 覆盖 JSON、Prompt 和结果状态；任务 4 覆盖 SSE、取消和总预算；任务 5 覆盖精简工作台；任务 6 覆盖评测、benchmark、ADR 和交付复现。
- 范围检查：没有加入真实摄取、检索、ACL、OIDC、队列、在线 fallback 或完整 RAG。
- 信任边界：调用方 evidence 只当作待核验数据；不生成可信 tenant/actor；工作台不持久化正文。
- 证据边界：identity 校验、人工质量判断、真实 LM Studio 观察和生产容量结论分别记录。
- 计划状态：本计划生成后仍需用户确认执行方式；本文件不表示实现已完成。
