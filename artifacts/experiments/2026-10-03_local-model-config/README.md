# 本地模型配置与指定模型切换

日期：2026-10-03。运行中的8001 API已重启，当前模型为
`qwen3-coder-next-reap-48b-a3b-mlx`。LM Studio原本已加载该模型；没有卸载共享模型、
重建索引、修改Embedding维度或停止其他API。

## 配置方法

本机.env已设置`LOCAL_LLM_CONFIG_FILE=config/local-llm.toml`。此文件优先于旧
模型/地址/调度环境变量，API Key仍只从LOCAL_LLM_API_KEY读取。TOML的model和
base_url必填；max_concurrency/max_queue_size默认2/8，必须为正整数。
未知字段、错误类型、无效TOML或缺失文件拒绝启动，不回退旧模型。

以后只修改TOML中的model为LM Studio实际模型ID，再执行：

```bash
bash start.sh stop
bash start.sh
```

不热更新，不自动下载模型。相对路径以项目源码根目录为基准；旧部署未启用
LOCAL_LLM_CONFIG_FILE时继续读取环境变量。Docker产品镜像本轮未验证文件挂载；
如使用容器需显式注入容器内绝对文件路径并挂载文件，或使用原环境变量方式。
model-registry.yaml只作能力与历史实验记录，不参与本机运行读取。

## 真实验证

`verification.json`来自运行中API的网络HTTP，未使用模型替身。

| 检查 | 结果 |
|---|---|
| Readiness / 前端 | HTTP200 / 200 |
| 公开caller-supplied摘录 | 2.971秒，answered/引用passed/stop，1条引用 |
| 股东产品检索问答 | 16.233秒，invalid_citations/failed/stop，0条有效引用 |
| 返回模型身份 | 两次均与TOML的coder模型一致 |

公开样本为“配置验证公开样本：审批期限为三个工作日。”，问题为审批期限是多少。
第二次是真实产品问题“平安银行公司股东数量及持股情况”，scopes9/dense64/
sparse0/RRF60/reranked20/parent5/BGE启用/mode normal，输出437 tokens。
报告仅保存状态与计数，不保存上传文档、模型答案或内部reasoning。

结论：配置文件读取与模型切换已成功；股东问题虽本次未再截断，
引用仍失败，尚未修复，不把HTTP200/stop当成该题业务成功。

## 回归

- RED：配置测试11 failed/1 passed，复现此前不读取TOML的问题。
- GREEN：配置/Settings/装配/Provider/启动定向39 passed。
- 全量339 passed/12 skipped；真实PG集成本轮未重跑。
- 局部Ruff、compile、git diff --check通过。

模型切换不能替代主体/日期归属与引用质量验收；历史通过结果仍按原模型解释。
