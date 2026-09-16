# 真实摄取基础设计

## 目标

本切片提供一个可重放、可审计、可测试的最小摄取闭环，支持 Markdown、HTML 和受控文本型 PDF。输入经过校验、原文落盘、解析规范化、结构化 lineage、确定性 parent/child chunk 后，状态写入现有 PostgreSQL 事实源，并把解析与 chunk 清单写入本地对象存储。

## 明确边界

实现：

- 扩展名、MIME、大小、UTF-8 和 PDF 可提取文本校验；
- Markdown/HTML/文本 PDF 解析为统一 `ParsedBlock`；
- 标题路径、稳定顺序和可取得的行号/页码 locator；
- 固定策略版本的 parent/child chunk 和稳定 ID；
- 原文与 parsed manifest 的确定性本地对象 key；
- PostgreSQL job、version、artifact 状态转换；
- 同租户幂等键复用、内容冲突检测和 FAILED job 显式 replay。

不实现：

- OCR、扫描 PDF、复杂表格或复杂版面恢复；
- Embedding、BM25F、Weaviate、Redis、Celery/RabbitMQ；
- ACL/OIDC、上传鉴权和完整 RAG 查询链；
- 跨对象存储与 PostgreSQL 的伪原子事务；
- 将 parsed artifact 或调用方 evidence 冒充检索结果。

## 数据流

1. `IngestionRequest` 校验输入格式、大小、编码和非空内容。
2. 原文按 `tenant + idempotency_key` 的哈希 key 写入 `ObjectStorage`。
3. 事务内创建或复用 tenant 内稳定 document、job 和内容版本；重复幂等键先比较原文 hash。
4. job 进入 `RUNNING`，version 进入 `PROCESSING`。
5. 解析器生成不可变 `ParsedBlock` 列表；chunker 生成 parent/child 清单。
6. parsed manifest 写入对象存储；事务内更新 `RAW/PARSED` artifact、version 和 job 状态。
7. 失败时保留原文和可识别的 `FAILED` 状态；只有显式 replay 才允许 FAILED job 回到 `PENDING`。

对象存储写入和数据库提交不宣称跨系统原子性。任一侧失败都会留下可重试或可清理的状态，并且错误原因不包含密码、完整 DSN 或原文正文。

## 统一中间表示

`ParsedBlock` 至少包含：

- `block_id`：由 parser 版本、顺序、locator 和文本 hash 派生；
- `block_type`：`heading`、`paragraph`、`list`、`code` 或 `page`；
- `text`：NFC 规范化后的文本；
- `heading_path`：从文档根到当前 block 的标题元组；
- `source_locator`：Markdown/HTML 记录稳定顺序，能可靠取得时记录行范围；PDF 记录 1-based 页码。

解析器不猜测字符 offset。HTML parser 只能可靠取得的行号才写入 locator，不能取得时只保留顺序。

## Chunk 策略

首期策略版本固定为 `parent-child-v1`：

- parent 默认最多 800 个 Unicode 字符；
- child 默认最多 320 个 Unicode 字符；
- 按空白/换行边界优先切分，单个超长 token 才按字符边界切分；
- 不做重叠，避免首期 lineage 和去重语义复杂化；
- parent/child 都保留来源 block IDs、heading path 和 locator。

`parent_id = sha256(version_id + strategy_version + parent_position + parent_text_hash)`；`child_id` 再加入 parent ID、child position 和 child text hash。相同 version、策略和内容重放必须得到相同 ID。

## 状态语义

- 新 job：`PENDING`；执行时：`RUNNING`；成功：`SUCCEEDED`；失败：`FAILED`。
- 新 version：`STAGED`；执行时：`PROCESSING`；解析和 manifest 完整后：`READY`。
- `RAW` 与 `PARSED` 可在本切片变为 `READY`；`EMBEDDING` 和 `SPARSE` 保持 `PENDING`，因此不会自动发布。
- 成功摄取不自动切换 `documents.current_version_id`，发布仍由既有发布事务单独完成。

## 错误分类

输入格式、编码、加密/空文本 PDF 和解析失败均归类为不可重试的 `IngestionValidationError` 或 `ParseError`；对象存储暂时失败、数据库暂时不可用等基础设施错误保留原始安全类型，由上层决定是否重试。FAILED job 的重放必须由调用方显式调用 replay API。

## 验证口径

- unit：格式校验、标题路径、locator、PDF 失败、chunk 边界和 ID 稳定性；
- integration：SQLite 离线状态契约与真实 PostgreSQL job/version/artifact 状态；
- failure：对象写入后解析失败、重复幂等键、内容冲突和 replay；
- 产物检查：原文 key、parsed manifest JSON、chunk lineage 和不自动发布。

