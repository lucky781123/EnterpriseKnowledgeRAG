# 评测集草案说明

状态：`待人工抽样确认`

## 使用方式

`dataset-draft.jsonl` 是第一版评测集草案，使用合成研发文档标识，不要求现在已经存在对应文档。请先抽样检查问题是否自然、难度是否合适、期望证据是否合理，再决定哪些问题进入 `development` 和 `frozen` 集合。

每条记录包含：

- `id`：稳定问题 ID；
- `category`：问题类别；
- `question`：用户问题；
- `answerability`：`answerable`、`unanswerable`、`conflict` 或 `forbidden`；
- `expected_evidence`：期望命中的文档/章节/字段；
- `tenant`、`user_roles`：权限场景；
- `gold_notes`：人工确认时需要关注的答案边界。

## 抽样确认规则

抽样时重点检查：

1. 问题是否能由研发文档回答，而不是依赖常识；
2. `expected_evidence` 是否足够具体；
3. `unanswerable` 是否确实缺少证据；
4. `conflict` 是否明确指出版本冲突；
5. `forbidden` 是否只因为权限不足而不能回答；
6. 是否覆盖中文、英文技术名、API、错误码、版本和多条件问题。

人工确认后，复制并标记为 `smoke`、`development` 或 `frozen`，不要直接覆盖草案。
