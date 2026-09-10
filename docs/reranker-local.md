# 本地 Reranker 运行方式

当前不要用 Ollama 的 `ollama run` 启动 `bge-reranker-v2-m3`。本机 Ollama `0.31.2-rc2` 会在 `llama-server` 初始化阶段触发 `n_outputs_max` 断言；该模型是 sequence-classification reranker，不是普通生成模型。

项目使用原生 Hugging Face `BAAI/bge-reranker-v2-m3`，通过 `transformers.AutoModelForSequenceClassification` 加载，支持 Apple Silicon 的 `mps`，没有 MPS 时自动回退到 CPU。

## 安装

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[reranker]'
```

`requirements-reranker.txt` 仍保留给已有本地脚本使用；新环境优先使用 `pyproject.toml` 中的 `reranker` optional extra。

## Smoke test

首次运行会从 Hugging Face 下载模型权重：

```bash
PYTHONPATH=src .venv/bin/python scripts/rerank_smoke.py
```

输出中分数最高的候选是最相关文档。`BGEReranker.rerank()` 输入一个 query 和候选文档列表，返回按分数降序排列的 `RerankResult`。
