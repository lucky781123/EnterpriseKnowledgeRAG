# 三类本地模型配置与切换验收

日期：2026-10-03。范围：配置切换、既有文档回填及公开样本端到端连通性。
不代表完整检索质量、生产容量或原股东问题修复。

## 当前配置

唯一运行配置文件为`config/local-llm.toml`，由`.env`的`LOCAL_LLM_CONFIG_FILE`
启用，优先于旧环境变量；配置错误拒绝启动，密钥不写入TOML。

| 角色 | 模型 | 服务 |
|---|---|---|
| LLM | qwen3-coder-next-reap-48b-a3b-mlx | LM Studio1234 |
| Embedding | feadxus/qwen3-embedding:4b，实测2560维 | Ollama11434 |
| Reranker | jina-reranker-v3-mlx | HTTP8003，POST /v1/rerank |

修改后stop/start，非热更新。当前受控API/UI为8001；用户8000和共享服务未停止。
当前API不在进程内加载BGE；旧部署兼容路径仍保留。

## 真实结果

- `reindex.json`：9份演示身份授权文档、179个Child已回填到新profile，
  9个scope成功。旧1024维索引/指针保留，不删除文档、不重复修改版本发布时间。
- `reindex-first-failure.json`：初次失败记录，保留追溯；已发布版本新增profile的
  发布守卫修复后才重新回填，不将初次失败写为成功。
- `upload.json`：公开TXT通过运行中API上传，HTTP201，published/active。
- `verification.json`：readiness全部ready、UI200；原文/manifest hash、lineage、
  两路chunk IDs、2560维向量、四类artifact ready和active pointer通过交叉核验。
- 用户绿茶示例请求真实8003服务，top_n=2，返回indexes `[0, 2]`，篮球候选未入选。
- 公开问题“MODEL_SWITCH_CHECK 审批期限是多少？”：Dense65、BM25F1、
  fused20、reranked20、RRF60、Jina使用成功、mode normal、fallback_reasons为空。
  回答`answered`、引用`passed`、`finish_reason=stop`，返回LLM ID与配置一致。
  最新模型生成约1.67秒；不将此值当全链路耗时或性能承诺。
- `public-answer.json`保存仅公开样本的完整回答：“MODEL_SWITCH_CHECK 审批期限为三个工作日。”
- 原股东题单独记录`shareholder_answer.passed=false`：
  生成约9.25秒/stop，但仍`invalid_citations / citation_validation=failed`。
  不归档私人正文/答案/模型reasoning，不把顶层passed扩展到该问题。

## 测试

- `pytest.txt`：离线全量371 passed / 12 skipped。12项因未注入PostgreSQL配置跳过。
- `postgres-tests.txt`：单独加载开发配置运行真实PostgreSQL集成，16 passed。
- 配置优先级、非法配置、HTTP响应身份/数量/分数、401/429/503、timeout/取消、
  profile物理隔离、查询维度、已发布version新增profile与失败不发布的回归均通过。
- 新增和相关模块Ruff通过，Skill suite通过。原`retrieval/product.py`与`weaviate.py`
  保留3条既有告警（UP037、TRY004、FLY002），未顺带修改；不宣称全仓lint通过。
- `compileall -q src tests scripts`与`git diff --check`通过；
  最终8001 readiness全部ready、前端HTTP200，受控服务保持运行。
- HTTP取消仅证明本地协程/传输取消，不证明8003服务端原生推理立即停止。

## 复现

本机已有模型服务/数据库，先加载`.env`，不打印任何凭据：

```bash
set -a
source .env
set +a
PYTHON=/Users/liuhui/opt/anaconda3/envs/enterprise-rag/bin/python
"$PYTHON" -m scripts.verify_model_switch
"$PYTHON" -m pytest -q tests/integration
```

验证脚本复用公开上传幂等键，不重复生成文档；会独立重测股东题，但只保存脱敏状态。
测试之前不要把开发环境变量注入全量确定性单测。

以后换Embedding需停止API、修改TOML模型/地址/实际维度，加载`.env`后：

```bash
"$PYTHON" -m scripts.reindex_local_models
"$PYTHON" -m scripts.reindex_local_models --apply
bash start.sh
```

回填工具限显式development匿名演示身份的当前授权文档；不是任意租户生产迁移工具。
仅更换LLM或Reranker不需回填，stop/start即可。不会自动下载模型，不清理旧索引。
