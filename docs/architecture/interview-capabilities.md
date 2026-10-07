# 高级大模型应用开发岗能力清单

本文只记录当前代码、测试和实验产物能够证明的能力。面试表达必须区分：

- **已实现**：代码路径存在，且有对应测试或契约。
- **已验证**：在注明的环境和数据范围内实际跑通。
- **待实验**：代码或计划已具备，但缺少目标环境证据。
- **阻塞点**：需要外部服务、凭据、权限或特定操作系统。
- **非目标**：本轮明确不做，不把它们当成隐藏完成项。

## 已实现与已验证

| 能力 | 当前实现 | 当前证据 |
| --- | --- | --- |
| RAG 自动评测 | `src/enterprise_rag/evaluation/rag.py` 和 `scripts/evaluate_rag.py`，覆盖 retrieval recall、citation precision/support、grounded answer、abstention、authorization，以及缺失/重复/不安全预测。 | `tests/evaluation/test_rag_evaluation.py`；报告只保存脱敏 ID 和统计值。 |
| 检索消融评测 | `src/enterprise_rag/evaluation/retrieval_ablation.py` 和 `scripts/run_retrieval_ablation.py` 统一比较 Dense、BM25F、候选并集、RRF、RRF+Reranker，并输出 Recall@K、MRR、nDCG、独有命中和外部延迟摘要。 | `tests/evaluation/test_retrieval_ablation.py`、`artifacts/experiments/2026-10-07_p1-retrieval-ablation/`；当前为离线契约 fixture，真实 Weaviate 消融仍待实验。 |
| 安全/可靠性回归门禁 | `regression_gate.py` 读取 pytest JUnit 结果，要求版本化清单中的 ACL、撤权、引用出站、Prompt Injection、重复投递、死信/replay、outbox、fencing 和阶段幂等测试全部通过。 | `tests/evaluation/test_regression_gate.py`、`docs/evaluation/p1-regression-manifest.json`、`artifacts/experiments/2026-10-07_p1-regression-gate/`；本地 12/12 场景通过，不等于 Linux/生产故障演练。 |
| 评测集冻结审计 | `dataset_audit.py` 检查 answerability/category 覆盖、answerable 空证据和人工审阅标记，区分结构可运行与 frozen ready。 | `tests/evaluation/test_dataset_audit.py`、`artifacts/experiments/2026-10-07_p1-dataset-audit/`；当前 40 条 draft 结构通过但未达到 frozen ready。 |
| 全链路 Observability | 请求级 `X-Request-ID`/`X-Trace-ID`、有界耗时 histogram、阶段 span、nearest-rank p50/p95、Provider usage/fallback/status 摘要；`/health/metrics` 只返回聚合信息。 | `tests/api/test_observability.py`、产品问答定向测试；Fake 应用层基线见 `artifacts/experiments/2026-10-07_p1-local-performance/`。真实模型/容量仍待实验。 |
| Groundedness/Citation/Abstention | `grounded` 必须同时满足 answered、引用通过且存在引用；拒答无引用才是 `abstained`；其余为 `ungrounded`。 | 产品 JSON/SSE 响应字段及 `tests/unit/test_grounding_and_prompt_injection.py`。 |
| Prompt Injection 防护 | evidence 按不可信 data 处理；高风险的指令覆盖、密钥外泄、shell/python/SQL/tool 执行模式在 Provider 调用前阻断，异常不回显原文。 | `tests/api/test_request_contracts.py` 和单元测试。 |
| 备份恢复能力 | 计划、manifest、恢复前写入门禁、PostgreSQL/MinIO/Weaviate/双 Redis 步骤、隔离目标和 restore-verify CLI 已实现。 | `tests/infrastructure/test_backup_plan.py`；真实托管恢复仍待验收。 |
| OIDC/JWT 与租户治理 | `PyJWTJWKSVerifier`、issuer/audience/JWKS/exp/iat 校验、服务端 membership resolver、租户 fixed-window 配额和脱敏审计事件已接入；OIDC 默认关闭且缺少依赖时 fail closed。 | `tests/unit/test_oidc_governance.py`、`tests/api/test_governance_api.py`。 |

## 面试演示命令

```bash
# 安装生产 OIDC 适配的可选依赖
python -m pip install -e '.[auth]'

# 运行离线 RAG 评测；predictions 文件只应包含脱敏字段
python -m scripts.evaluate_rag \
  --dataset docs/evaluation/dataset-draft.jsonl \
  --predictions /private/tmp/rag-predictions.jsonl \
  --output /private/tmp/rag-report.json

# 只生成备份/恢复计划，不连接真实基础设施
python -m scripts.backup_restore plan \
  --directory .local/backups/interview-demo \
  --plan-action restore-verify

# 查看运行态的脱敏指标和治理计数
curl http://127.0.0.1:8001/health/metrics
curl http://127.0.0.1:8001/health/governance
```

## 仍未完成的真实验收

- 托管 MinIO 与 Weaviate 的真实快照、跨实例恢复和权限验证。
- 正式 IdP 的 OIDC token、JWKS 轮换、membership 数据源、账号停用和撤权演练。
- Linux `prefork` Worker kill/redelivery 故障演练。
- 50 QPS、10 万文档容量、P95、RPO/RTO 的目标实验。

本轮明确跳过 Linux 故障演练和容量压测；Fake、SQLite、local object storage、
macOS `solo` Worker、配置解析和离线 plan 都不能替代上述真实证据。Prompt
Injection 检测器也只是防护层，不等于完整安全证明；Groundedness 指标不等于事实正确性。
