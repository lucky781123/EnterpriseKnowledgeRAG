# BM25F 中文分词与查询预处理对照实验

本实验固定 Dense、字段权重、candidate K 和 RRF 公式，只比较 `word + identity`、`trigram + identity` 与 `word + cjk_bigram`。

```bash
WEAVIATE_BASE_URL=http://127.0.0.1:8080 \
OLLAMA_BASE_URL=http://127.0.0.1:11434 \
/Users/liuhui/opt/anaconda3/envs/aitest/bin/python scripts/weaviate_bm25f_chinese_evaluation.py
```

数据仍是固定合成小样本，不能代表真实企业语料、生产容量或默认参数。
