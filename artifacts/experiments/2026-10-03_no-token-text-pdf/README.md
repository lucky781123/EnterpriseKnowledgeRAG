# 2026-10-03 免 Token TXT / 大 PDF 真实验收

## 结论

本机显式 `P0_ANONYMOUS_ENABLED=true`，实际工作台不显示 Token 控件，
上传时 Source ID、标题、幂等键全部留空，自动生成必要业务字段。
使用真实 Chrome、网络 HTTP、PostgreSQL、Weaviate、Ollama、BGE、LM Studio，
不是 mock 回答，不以 HTTP 200 代替业务断言。

| 样本 | 字节数 | 上传 | 状态 / 发布 | 检索 / 回答 |
|---|---:|---|---|---|
| `samples/polaris.txt` | 见 samples.json | 201 | ready / published / active | Dense、BM25F、RRF60、BGE；answered / citation passed |
| `samples/vega.pdf` | 308279 | 201 | ready / published / active | Dense、BM25F、RRF60、BGE；answered / citation passed |

PDF 是合法一页文本型 PDF，包含真实 RGB 图像资源和可提取英文文字。
文件超过 128 KiB；不是尾部填充，不代表 OCR、复杂表格或大容量文档验证。
TXT 是 UTF-8 中文公开合成样本，保留字面文本语义。

真实回答：

- TXT：“Polaris 项目的备份保留期为 37 天，每天凌晨 03:15（时区 Asia/Shanghai）执行备份。”
- PDF：“The Vega maintenance window is every Friday from 21:10 to 21:50 UTC.
  The rollback owner is the platform operations team.”

两份文档均通过原文/manifest hash、Parent/Child lineage、active pointer、
两路 chunk IDs/count、实际 1024 维向量交叉核验。
每份样本为 1 Parent / 1 Child；TXT 为 3 blocks，PDF 为 1 page block。
这证明链路连通，不证明大规模切片质量或完整企业数据质量。

## 证据索引

- `acceptance.json`：最终汇总、网络 readiness、无 Authorization 状态查询、
  跨站 queue 403、普通 JSON 128 KiB 限制仍为 413。
- `txt-upload.json`、`pdf-upload.json`：真实浏览器 SSE 收到的产品上传 201。
- `txt-answer.json`、`pdf-answer.json`：实际模型消息、引用、完整检索 trace。
- `txt-persistence.json`、`pdf-persistence.json`：真实 PG / 对象 / 双路索引核验。
- `txt.json`、`pdf.json`：浏览器免 Token、元数据留空、桌面/移动和页面错误断言。
- `*-desktop.png`、`*-mobile*.png`：真实 UI 截图，不输出凭据。
- `http-success.log`：从受控服务日志中摘录的真实上传/状态/问答访问行。
- `samples.json`：公开样本字节数、sha256、PDF 文字和图像资源断言。
- `fallback.json`：隔离 factory 的 ASGI HTTP 故障注入；其余依赖仍真实。
- `pytest-offline.txt`：303 passed / 12 skipped；未设置 DSN 的真实 PG 测试明确跳过。
- `pytest-postgres.txt`：单独加载开发环境后真实 PostgreSQL 16 passed。
- `replay.json`、`workbench-failed.png`：首次 TXT 服务注册缺失失败及修复 replay 记录。
  失败截图仅用于追溯，不是成功证据。

## 本轮修复及边界

1. 原 Gradio 临时上传路径被普通 128 KiB middleware 拦截。
   现精确文件路径预算为 10 MiB + 64 KiB multipart 开销；
   Gradio 和摄取本体仍限制单文件 10 MiB，普通 API 仍为 128 KiB。
2. TXT 校验白名单、MIME、独立保真 parser、IngestionService 注册表全部补齐。
3. UI 自动补齐空元数据，相同来源/标题/内容使用稳定幂等键；
   摄取 FAILED 仍需显式 replay，本轮仅恢复哈希精确匹配的公开样本。
4. 当前本机免 Token 使用固定 `local-demo/local-demo-user`、editor/reader。
   不解析 Token、不代签 Token，不接受客户端 tenant/actor；ACL 仍全程执行。
   模式默认关闭、仅 development，真实 socket peer/Origin 为本机。
   Uvicorn 关闭 proxy headers；Gradio queue 与 API 共用本机边界。
5. 7860 兼容书签返回 307 到统一 8001 UI，避免第二套配置漂移。
   用户原 8000、模型和数据库未停止。
6. Embedding、Dense、Sparse、BGE 单项网络故障各降级成功：
   HTTP 200 / answered / 引用 passed，每项真实本地 LLM 调用 1 次。
   双路全部不可用返回 503，真实 LLM 调用 0 次。
   这是故障注入证据，不是关闭共享服务的证据。
7. 无授权证据不允许纯模型自由回答；本轮没有新增在线/第二模型 fallback。
   历史默认鉴权模式下的撤权、跨 tenant、认证和模型故障证据仍在
   `../2026-10-03_runtime-acceptance/`，不能把当前固定演示身份当作 OIDC。

## 复现

前置：配置好的本地开发环境、真实模型/数据库可用，根 `.env` 显式设置
`P0_ANONYMOUS_ENABLED=true`。设置变更后执行 `bash start.sh stop` 再 `bash start.sh`。
其他机器 `.env.example` 默认 false，不对外部署此模式。

```bash
bash start.sh
PYTHON=/Users/liuhui/opt/anaconda3/envs/enterprise-rag/bin/python
"$PYTHON" -m scripts.verify_no_token prepare
```

浏览器自动化使用本机 Chrome 和已安装的 Playwright，固定公开样本目录：

```bash
export ACCEPTANCE_REPORT_NAME=2026-10-03_no-token-text-pdf
export PLAYWRIGHT_MODULE=/Users/liuhui/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright
export CHROME_EXECUTABLE='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
NODE=/Users/liuhui/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node
ACCEPTANCE_CASE=txt \
ACCEPTANCE_FILE="$PWD/artifacts/experiments/$ACCEPTANCE_REPORT_NAME/samples/polaris.txt" \
ACCEPTANCE_QUESTION='Polaris 项目的备份保留期是多少天，执行时间是什么？' \
ACCEPTANCE_EXPECT='37|03:15' "$NODE" scripts/verify_workbench.cjs
ACCEPTANCE_CASE=pdf \
ACCEPTANCE_FILE="$PWD/artifacts/experiments/$ACCEPTANCE_REPORT_NAME/samples/vega.pdf" \
ACCEPTANCE_QUESTION='What is the Vega maintenance window and who owns rollback?' \
ACCEPTANCE_EXPECT='21:10|21:50' "$NODE" scripts/verify_workbench.cjs
set -a
source .env
set +a
"$PYTHON" -m scripts.verify_no_token verify
"$PYTHON" -m pytest -q tests/integration \
  | tee artifacts/experiments/2026-10-03_no-token-text-pdf/pytest-postgres.txt
```

干净 shell 中执行全量，避免开发变量污染确定性单测；之后汇总：

```bash
"$PYTHON" -m pytest -q \
  | tee artifacts/experiments/2026-10-03_no-token-text-pdf/pytest-offline.txt
"$PYTHON" -m scripts.verify_no_token finalize
```

本实验只处理公开合成样本，不读取用户私人文档或输出 Token/DSN/内部 reasoning。
样本保留供用户验收；普通匿名 UI 上传的用户文档不进入公开实验目录。
