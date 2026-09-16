# ADR-010：F2-G 原文摘录式回答边界

## 状态

Accepted，2026-09-14。

## 决策

本阶段采用 F-2 Evidence Bundle：调用方提交 `question + evidence`，服务端只负责输入预算、Prompt 组装、真实 Provider 调用和摘录 identity 校验。`context_source` 固定为 `caller_supplied`。

## 不包含的能力

本阶段不实现真实摄取、解析、切块、Embedding、Weaviate 检索、ACL/OIDC、队列、跨 Provider fallback 或在线模型调用。调用方 evidence 不被视为租户身份、授权声明或已完成检索。

## 理由与代价

该边界可以先用 LM Studio 真实模型验证服务契约和引用核验，减少基础设施耦合；代价是无法证明上游检索质量、来源真实性或完整 RAG 正确性。后续接入企业数据时必须重新设计身份、ACL 和 lineage。

## 验收口径

程序只接受 `evidence.text[start:end] == quote` 的引用。该结果证明引用身份一致，不证明事实支持、语义相关性或权限正确。
