# 实验结论

## 可确认结论

- F2-G 的 evidence identity 校验、LM Studio OpenAI-compatible `complete/stream` adapter、FastAPI JSON/SSE 契约、scheduler/deadline/取消清理、摘录 evaluator 和精简本地工作台均已有离线自动化覆盖。
- 离线回归为 `101 passed, 7 skipped`；Ruff、`compileall`、Compose 静态配置和 `git diff --check` 均通过。跳过项是需要显式 PostgreSQL DSN 的集成测试，不能被解释为 PostgreSQL 失败。
- Fake 基线 10/10 成功，completion P50/P95 约为 `0.016/0.058 ms`。这是确定性应用层上限参考，和真实模型结果分开保存、分开解释。
- 2026 年 9 月 14 日，本机 LM Studio `/v1/models` 返回 HTTP 200，共发现 19 个模型，目标模型存在；因此本次真实本地实验确实命中了目标模型配置。

## 两组真实本地结果

### `max_tokens=128`

40 个正常样本（JSON/SSE 各 20，客户端并发 1/2 各覆盖）全部以 `finish_reason=length` 结束，业务状态为 `incomplete`，错误分类为 `truncated`。这证明请求、传输和模型生成路径可用，但 128 token 预算不足以形成完整摘录回答；成功率和成功延迟因此按 0 记录，不能把 HTTP/Provider 调用成功当作业务成功。

主动取消样本为 2/2 观察到 `cancelled`。`completion_ms` P50/P95 约为 `1001.1/1002.5 ms`，cleanup P50/P95 约为 `0.57/0.63 ms`。cleanup 只说明应用侧资源得到回收，不说明 LM Studio 或上游 GPU 已停止生成。

### `max_tokens=256`

40 个正常样本全部以 `finish_reason=stop` 结束，39 个为 `answered + citation_validation=passed`，1 个为合法的 `abstained + citation_validation=not_applicable`。这能证明当前固定 evidence 下的完整终态和引用身份校验可观察；不能证明语义正确、事实真实性、检索召回质量或人工验收通过。

JSON 并发 1/2 的 completion P50/P95 分别约为 `12.56/25.15 s` 和 `10.29/17.73 s`，完成吞吐约为 `0.398/0.564 req/s`。SSE 并发 1/2 的 completion P50/P95 分别约为 `12.57/25.19 s` 和 `9.72/17.56 s`，完成吞吐约为 `0.397/0.569 req/s`；SSE TTFT P50/P95 分别约为 `12.10/24.73 s` 和 `9.42/16.95 s`。这些是当前机器、当前 LM Studio 进程、当前模型、当前 Prompt 和当前配置下的客户端观测，不是容量承诺。

本次 JSON 响应均观察到 usage；SSE 响应均未观察到 usage，因此没有从字符数、chunk 数或时间推算 token 吞吐。

## 明确边界

本产物不宣称真实文档摄取、解析、切块、Embedding、索引、检索、ACL、生产容量、SLO、成本、功耗、GPU 显存、上游停止生成语义或在线 Provider 可用性。下一阶段若接入真实摄取和检索，必须重新加入 tenant/ACL、chunk lineage、检索质量、引用真实性和可重放任务证据；真实模型 benchmark 也应在模型、Prompt、数据集或运行时变化后重新运行。
