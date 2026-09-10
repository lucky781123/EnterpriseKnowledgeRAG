# 贡献指南

## 开发环境

项目要求 Python 3.11 或更高版本。建议使用独立虚拟环境：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

需要本地 BGE Reranker 时，再安装 `python -m pip install -e '.[reranker]'`。

## 提交前检查

```bash
python -m ruff check .
python -m pytest -q
python -m compileall -q src tests
```

不要提交 `.env`、API Key、密码、token、模型权重、缓存、生成物或本地对象存储内容。

## 变更原则

- 先补充能复现行为的测试，再修改实现。
- 保持 Provider、权限、索引和业务对象边界清晰。
- 不把 Fake、本地单机或合成数据结果描述成生产结论。
- 重要技术选择记录到 `docs/adr/`，并说明代价、验证方法和重审条件。
