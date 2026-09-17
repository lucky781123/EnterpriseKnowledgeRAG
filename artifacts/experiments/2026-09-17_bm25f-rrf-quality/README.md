# BM25F/RRF 固定小数据集质量实验

本目录记录真实 Ollama Embedding 与 Weaviate 1.27.10 上的 Dense、BM25F 和 RRF 对照。

```bash
WEAVIATE_BASE_URL=http://127.0.0.1:8080 \
OLLAMA_BASE_URL=http://127.0.0.1:11434 \
/Users/liuhui/opt/anaconda3/envs/aitest/bin/python scripts/weaviate_bm25f_rrf_evaluation.py
```

该合成小数据集只用于发现通道互补和参数敏感性，不代表生产语料质量或容量。
