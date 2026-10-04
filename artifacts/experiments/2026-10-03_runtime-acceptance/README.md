# 2026-10-03 本地真实运行验收

## 结论

本地P0真实链路通过：Markdown输入 → 自研parser → ParsedBlock →
Parent/Child → 原文/manifest → PostgreSQL状态 → Ollama →
Weaviate Dense/BM25F → 双路完整性与事务发布 →
Bearer/ACL scope → 双路召回 → RRF60 → BGE → Parent/ACL →
真实LM Studio → answered/逐字引用 → 出站ACL复核。

公开问题：“Aurora 项目的发布冻结窗口是什么时间？”
真实输出：“Aurora 项目的发布冻结窗口是每周三 20:00-22:00，时区为 Asia/Shanghai。”
具体逐字引用、usage、模型和trace见 `answer.json`，不保存内部reasoning。

| 证据 | 验证内容 |
|---|---|
| `metrics.json` | 网络HTTP readiness200、上传201/published/active/四产物ready、状态200、正常query200/answered/引用passed、跨tenant与撤权404、重授200 |
| `persistence.json` | 原文/manifest hash、2 blocks、1 Parent/1 Child、Dense/Sparse各1对象、chunk IDs/count、实际1024维、active pointer |
| `answer.json` | 网络HTTP真实模型完整回答；正常trace两路均命中、fused/reranked非零、BGE=true |
| `fallback.json` | 8类ASGI HTTP故障注入，其他依赖仍真实连接；4类降级各调用真实LLM1次 |
| `fallback-output.txt` | 各case完成后逐项打印的真实输出 |
| `browser.json` | 真实Chrome工作台上传、状态、问答与完整SSE trace；桌面1440×1000、移动390×844、零pageerror/横向溢出 |
| `browser-answer.json` | 浏览器实际收到的完整Gradio SSE回包，仅含公开合成样本答案 |
| `workbench-*.png` | token清空后的桌面/移动截图；failed截图保留最后一次诊断，不能当最终成功图 |
| `pytest-offline.txt` | 干净环境全量279 passed、12 skipped |
| `pytest-postgres.txt` | 显式开发配置、真实PostgreSQL集成16 passed |
| `final-checks.json` | 产物identity一致、8个故障用例、未检出配置凭据字面量、token0600、前后端200 |

Browser样本与网络smoke分开：在同租户额外上传公开合成文档，
检索scope>=2，证明同collection多文档发布不互相污染。
浏览器故障注入不使用生产故障；正常UI通过不等于前端全功能/企业UX完成。

## 故障矩阵

| 注入阶段 | HTTP | 结果 | 实际LLM调用 |
|---|---:|---|---:|
| embedding网络故障 | 200 | Sparse-only、degraded、引用passed | 1 |
| Dense网络故障 | 200 | Sparse可用、degraded、引用passed | 1 |
| Sparse网络故障 | 200 | Dense可用、degraded、引用passed | 1 |
| BGE网络故障 | 200 | 保留RRF、reranker_used=false、引用passed | 1 |
| 双路全故障 | 503 | 无可用检索依赖，停止 | 0 |
| Dense非retryable认证故障 | 503 | 不借另一通道掩盖认证失败 | 0 |
| 模型不可用 | 503 | 不伪造回答；模型方法替换为抛异常 | 0 |
| 无文档ACL | 404 | 无证据，不调用LLM | 0 |

注入函数只抛异常，不返回模拟候选或模拟答案；这些是ASGI HTTP实验，
不是关闭真实Ollama/Weaviate/LM Studio的演练。正常路径使用正在监听8001的网络HTTP。
“本地模型兜底”仅指保留授权证据后用真实本地模型生成；没有证据不能自由回答。

## 实际运行配置

- Python3.11.15，Conda `enterprise-rag`；Gradio6.29.0。
- PostgreSQL16，Weaviate1.27.10；对象存储为项目 `.local/p0-object-storage`。
- Ollama `bge-m3:latest`，`OLLAMA_EMBEDDING_DIMENSION=1024`。
- BGE `BAAI/bge-reranker-v2-m3`，CPU。
- LM Studio `qwen3.6-35b-a3b-claude-4.7-opus-reasoning-distilled-vlm-mlx-oq6`。
- HTTP smoke/故障查询max_tokens1024。浏览器使用工作台控件，准确返回内容以SSE回包为准。
- 仅本机API+真实依赖容器验收；本轮未构建/验收p0-api Docker镜像。

没有在产物中保存DSN、密码、token、私人正文、对象key或内部collection名称。
`answer.json`保存公开答案前，runner必须核对原文精确等于脚本 `_SMOKE_DOCUMENT`。
交互token只在 `.local/runtime/acceptance-token.txt`（0600），过期重跑runner。

## 复现

启动前先配置Docker、Ollama、LM Studio和本地模型，保留已有 `.env`：

```bash
bash start.sh
# 按脚本输出的实际API地址设置参数，不抢占8000
set -a
source .env
set +a
PYTHON=/Users/liuhui/opt/anaconda3/envs/enterprise-rag/bin/python
"$PYTHON" -m scripts.local_p0_smoke --api-base-url http://127.0.0.1:8001 \
  --timeout-seconds 150 --output artifacts/experiments/2026-10-03_runtime-acceptance/metrics.json
"$PYTHON" -m scripts.verify_local_runtime \
  --report artifacts/experiments/2026-10-03_runtime-acceptance/metrics.json \
  --api-base-url http://127.0.0.1:8001
"$PYTHON" -m pytest -q tests/integration
```

离线全量在**未加载开发.env**的干净shell运行 `python -m pytest -q`。
不能把全部模型环境注入确定性单测，Settings默认值测试依赖隔离。
集成测试使用独立临时数据库，结束后清理该临时数据库，不清理用户固定schema。
smoke会保留自身测试tenant/文档，不删除用户数据；不是零副作用操作。

浏览器脚本需Node+Playwright和Chrome；本机bundled Node路径见Codex workspace dependencies。
可设置 `PLAYWRIGHT_MODULE` 为已安装Playwright路径、
`CHROME_EXECUTABLE` 为本机Chrome，然后 `node scripts/verify_workbench.cjs`。
脚本只用公开Buffer上传，读取私有token且不输出，最后关闭自建浏览器。
长JSON编辑器采用虚拟滚动，完整trace以实际SSE回包断言，不能依赖body文本含所有行。
EventSource长连接优先通过CDP逐条接收；只有流结束后才可使用response.text。
移动长截图曾有拼接画面，最终保存字体/布局稳定后的视口截图。

## 本轮缺陷与处理

1. 恢复被移走且仍被引用的 `compose.p0.yaml`；其他历史脚本保留不用。
2. 原装配固定4096维与实际bge-m3/1024不符，上传503；新增显式维度配置，
   profile仍包含模型/维度，保留完整性守卫。
3. 工作台上传未按扩展设置MIME；补齐md/html/pdf映射。
4. smoke以HTTP200当成功；增加answered/引用/双路/RRF/BGE及发布状态断言。
5. 单依赖故障整条链失败；新增有授权证据的有限降级与trace。
6. 检索/引用复核不受完整deadline；现在共享总预算，超时504且取消传播。
7. 同租户第二份文档上传503：Dense/Sparse REST对象列表未执行where，
   完整性混入其他generation；cursor分页后精确过滤，再核验Sparse tenant/profile。
   新增分页、跨generation污染和scope损坏测试，并通过同租户真实UI验证。
8. 新测试异常构造遗漏safe_message、浏览器Slider定位和CodeMirror虚拟滚动
   属验收脚本问题，修正后再跑，不计为产品成功或模型质量。

新增启动/验收脚本、相关新测试局部Ruff通过，Skill validator、compileall、
diff检查另行记录。全仓旧代码存在新版Ruff规则告警，不宣称全仓lint通过。
Starlette/httpx与PyPDF2有弃用warning，本轮未升级。

## 不外推的范围

只有公开小样本的功能闭环，不是完整检索质量/faithfulness/容量或生产安全审计。
Parser/Parent/Child多样化边界主要由单测证明，本轮真实上传样本是Markdown。
Sparse-only只有Child正文且部分locator缺失；完整性cursor扫描tenant collection、
多scope串行/N+1、BGE线程取消不停止底层计算均未完成性能治理。
OIDC、Redis、Worker、OCR、Git、Agentic、HA、RPO/RTO、SLO和在线Provider fallback未完成。
