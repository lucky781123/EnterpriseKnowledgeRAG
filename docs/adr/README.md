# ADR 规范

每个重要架构决策使用一个独立 ADR，文件名建议：`ADR-NNN-short-name.md`。

## 必填结构

```markdown
# ADR-NNN: 标题

- 状态：提议 / 已接受 / 待验证 / 已废弃
- 日期：YYYY-MM-DD
- 影响范围：

## 背景
## 问题
## 候选方案
## 决策
## 性能影响
## 安全影响
## 运维影响
## 验证计划
## 重审条件
## 相关实验与证据
```

## 首批 ADR 主题

- Embedding 使用 Ollama；Reranker 使用进程内 `BGEReranker + SyncRerankerAdapter`；最终 LLM 使用 LM Studio/在线 Provider。
- `omlx` 作为小模型运行时的候选边界。
- LangChain 仅文本处理适配层。
- Dense + BM25F 分别查询、RRF 和 Reranker。
- Parent/Child 切块和版本元数据。
- Linear 优先、有界 Agentic。
- PostgreSQL 作为业务权威和 Checkpoint 存储。
- 对象存储实现重新选型。
- 本地优先评测、在线抽样、Fake 压测。
- Minikube 的验证边界与云上演进。
