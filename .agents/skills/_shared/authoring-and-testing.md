# Skill 编写与验证方法

## 开发循环

1. 写一个能暴露错误决策的压力场景，并记录未加载 Skill 时的 baseline 行为。
2. 只增加能纠正已观察错误的 Skill 规则，避免收集泛化口号。
3. 重新运行相同场景，检查决策、产物和验证证据是否改变。
4. 增加相邻边界或对抗场景，确认规则不会误伤其他 Skill 的职责。
5. 执行 `python .agents/skills/_shared/scripts/validate_suite.py` 和项目测试。

## 场景编写标准

- `request` 模拟真实用户任务，不直接透露期望答案。
- `setup` 只提供完成任务所需事实，不能暗示修复方式。
- `expected_decisions` 描述可观察决策，不规定固定措辞。
- `required_invariants` 必须来自 suite manifest 或对应 `skill-contract.json`。
- `forbidden_actions` 列出最危险的错误捷径。
- `verification` 必须能通过测试、查询、日志或人工检查确认。

## 通用开发提示词

```text
使用目标 Skill 完成给定任务。先读取项目事实和 Skill 声明的相关 references。

在修改前输出：职责边界、已知事实、需要验证的假设、失败场景和成功标准。
先写能复现目标行为或风险的测试，确认测试因正确原因失败，再做最小实现。

必须保持 RequestContext、AuthorizationContext、证据 lineage、剩余 deadline 和结构化错误；
不得伪造 API，不得让 fallback 放宽 ACL，不得在证据不足时生成伪装成知识库答案的内容。

完成后运行与改动直接相关的测试、Skill suite validator 和项目已有检查，
逐项报告命令、结果、未验证内容与剩余风险。
```

## 评审提示词

```text
独立审查目标 Skill 对压力场景的处理。不要接受“应该正确”的陈述，只接受新鲜验证证据。
优先寻找：职责越界、跨租户泄漏、授权时序错误、丢失 lineage、重置 deadline、无限重试、
静默降级、敏感信息进入日志、把检索内容当指令、指标无法复现和只覆盖 happy path。
按严重程度给出文件与场景定位，并说明能证明问题的最小反例。
```
