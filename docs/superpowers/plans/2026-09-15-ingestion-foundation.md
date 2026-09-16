# 真实摄取基础实施计划

## 文件边界

- `src/enterprise_rag/ingestion/contracts.py`：请求、解析 block、chunk 和结果的不可变数据契约。
- `src/enterprise_rag/ingestion/validation.py`：媒体类型、大小、编码和 PDF 入口校验。
- `src/enterprise_rag/ingestion/parsers.py`：Markdown、HTML、受控文本 PDF 解析与规范化。
- `src/enterprise_rag/ingestion/chunking.py`：确定性 parent/child chunk 和稳定 ID。
- `src/enterprise_rag/ingestion/service.py`：对象存储、数据库状态和幂等/replay 编排。
- `src/enterprise_rag/ingestion/__init__.py`：公开稳定入口。
- `tests/unit/test_ingestion_*.py`：纯函数与失败边界测试。
- `tests/integration/test_ingestion_postgres.py`：真实 PostgreSQL 状态证据（没有 DSN 时跳过）。

## 执行顺序

1. **契约和校验 RED/GREEN**
   - 先写未知格式、超限、非法 UTF-8、空输入和加密 PDF 的失败测试。
   - 实现 `IngestionRequest`、错误分类和输入校验。
   - 验证：定向 validation 测试通过，错误不包含敏感配置。
2. **解析器 RED/GREEN**
   - 先写 Markdown 标题/代码/列表、HTML 标题路径和 PDF 页码 lineage 测试。
   - 实现统一 `ParsedBlock` 和三个 parser；扫描 PDF、空 PDF 和加密 PDF 必须明确失败。
   - 验证：解析结果可 JSON 序列化，locator 不虚构字符位置。
3. **chunker RED/GREEN**
   - 先写 parent/child 数量、边界、来源 lineage 和重复运行 ID 相等测试。
   - 实现 `parent-child-v1`，保存策略版本和文本 hash。
   - 验证：相同输入得到字节级一致 manifest，策略边界可解释。
4. **摄取 service RED/GREEN**
   - 先写 raw/parsed artifact 落盘、job/version 状态、幂等复用、内容冲突和 FAILED replay 测试。
   - 实现最小同步执行器 `ingest`、`create_job`、`run_job`、`replay_failed`。
   - 验证：成功版本为 READY 但不发布；失败状态可识别且只能显式 replay。
5. **真实 PostgreSQL 证据**
   - 在用户已配置的专用 DSN 中运行摄取集成测试；每个测试只创建随机临时数据库。
   - 验证 job/version/artifact 状态、同租户幂等约束和重放行为。
6. **文档与回归**
   - 更新 README、roadmap、acceptance matrix、PROJECT_CONTEXT、handoff 和 planning 文件。
   - 运行定向 pytest、全量 pytest、Ruff、compileall、Compose 静态检查和 `git diff --check`。

## 明确不做

不新增 chunk 数据库表、不接入 OCR/Embedding/Weaviate/ACL/OIDC/Celery，也不把本地 parsed manifest 当成检索索引。

