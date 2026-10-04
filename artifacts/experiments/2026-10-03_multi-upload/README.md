# 多文件与无大小上限验收

本目录只使用公开合成样本，保存其真实回答和引用，不包含用户私人文档、
token或模型内部reasoning。

验证目标：

- 工作台 `gr.File(file_count="multiple")` 一次选择多个文件；
- 每个文件独立上传并生成独立 `document_id`、source/idempotency；
- Gradio兼容上传路由接受超过1000个文件和超过10 MiB的单文件；
- Gradio `queue/join` 批量文件路径元数据不再受普通128 KiB JSON预算限制；
- 产品摄取链仍执行格式、解析、切片、向量、稀疏索引和发布校验。

普通 JSON 请求体仍保持 128 KiB；“无大小上限”只表示本项目不设置产品
multipart 文档的应用层大小上限，实际仍受机器磁盘、内存、网络、解析器和
下游模型处理能力限制。

## 真实结果

- Chrome一次选择 `alpha.txt`、`beta.md`、`large-policy.pdf`；
- 3个独立文档均返回 `201`，`published/active/ready`；
- 最大文件 `12583979` bytes（约12 MiB，含真实图像与可解析文字）；
- DB、原文/manifest hash、Parent/Child lineage、Dense/BM25F索引、
  1024维向量和active pointer交叉核验通过；
- 默认 `2048` tokens 查询返回 `answered`、`citation_validation=passed`、
  `finish_reason=stop`，回答包含每周六 `22:20` 至 `22:50 UTC` 和平台运维团队；
- 本次查询scopes7、dense24、sparse0、RRF融合20、BGE20、Parent5。
  Sparse索引已核验存在，但中文整句对英文PDF未产生BM25F命中，不冒称双路命中；
  此前中文TXT/英文PDF显式词法查询的双路命中证据在no-token-text-pdf目录。
- 普通JSON `129 KiB` 仍返回 `413`，跨站queue仍返回 `403`；
- 最终默认私人“试用期”查询约16.913秒、answered、5引用passed、finish stop；
  `private-query-summary.json`仅存状态/计数，不含回答或文档正文。
  这不是对所有相关条文语义覆盖的人工验收。
- 离线回归 `327 passed, 12 skipped`；真实PostgreSQL集成 `16 passed`；
  结果见 `pytest-offline.txt` / `pytest-postgres.txt`。

证据：`browser.json`、`browser-upload.json`、`browser-answer.json`、
`api-answer.json`、`persistence.json`、`acceptance.json`、`http-success.log`、
`desktop.png`、`mobile.png`。`browser-failure.json`保留先前失败记录，
最新成功以 `browser.json` 和 `acceptance.json` 为准。

## 复现

```bash
bash start.sh
PLAYWRIGHT_MODULE=/Users/liuhui/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright \
CHROME_EXECUTABLE='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \
node scripts/verify_multi_upload.cjs
set -a
source .env
set +a
/Users/liuhui/opt/anaconda3/envs/enterprise-rag/bin/python -m scripts.verify_multi_upload
```

浏览器只读取本目录三个公开样本。兼容上传路由复用实测Gradio6.29.0的内部
multipart parser；升级Gradio必须重跑1001文件、queue大元数据与真实浏览器测试。
工作台顺序逐文件摄取，不无限并发；产品API/解析仍会读取单份完整内容到内存，
取消大小限制不表示已完成超大文件流式解析或海量文档容量测试。

## 失败与修复

- 首次脚本只抓EventSource，而Gradio用fetch SSE；补真实响应克隆读取。
- 原检索按文档顺序拼接后截断，PDF未进入BGE；新增Dense/Sparse两项RED，
  修复融合前的全局通道排名。未改ACL、profile或查询候选数量。
- 生成器使用process_generating差量，脚本只读process_completed漏结果；
  补差量数组解析，HTTP与浏览器分别通过。先前502为真实失败，不算成功。
- 持久化汇总误读citation.document_id和分隔符，改按source的`#`分隔核验。
