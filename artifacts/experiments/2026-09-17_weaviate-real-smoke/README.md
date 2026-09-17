# Weaviate 真实 Dense/BM25F Smoke

本目录记录 2026-09-17 在项目 Compose Weaviate `1.27.10` 上执行的脱敏 smoke。

## 复现

```bash
docker compose up -d weaviate
WEAVIATE_ENABLED=true WEAVIATE_BASE_URL=http://127.0.0.1:8080 \
  /Users/liuhui/opt/anaconda3/envs/aitest/bin/python scripts/weaviate_smoke.py
WEAVIATE_ENABLED=true WEAVIATE_BASE_URL=http://127.0.0.1:8080 \
  /Users/liuhui/opt/anaconda3/envs/aitest/bin/python scripts/weaviate_bm25f_smoke.py
```

## 产物

- `dense-metrics.json`：Dense collection、batch、完整性、nearVector query 和 cleanup。
- `bm25f-metrics.json`：字段投影、BM25F weighted query、tenant/version 负过滤和 cleanup。

报告不保存 API key、DSN、密码、真实企业正文或完整模型回答。

