# 股东问答模型诊断（2026-10-03）

本轮是诊断，不是修复验收。运行中的8001 API、`.env`、Embedding、Reranker和
共享LM Studio加载状态均未修改。报告不保存文档正文、模型答案或内部reasoning。

## 结果

- 用户原日志：当前蒸馏模型`finish_reason=length`，completion/reasoning均2047，
  反复重复推理，未形成最终结构化回答。该日志证明本次生成退化，不证明服务代码
  无限循环，也不能据此判定Embedding/Reranker故障。
- `inspection.json`：通过真实产品factory检索并拦截模型调用，取得5条授权证据，
  来自2份文档；3条来源标题含“平安”，另2条不含。当前模型输入只有内部
  document/chunk ID，没有文档标题。存在来源识别风险，但标题不匹配本身不能证明
  一定来自另一家银行，本轮未完成跨公司语义核验。
- `sampling-probe.json`：相同真实检索证据、相同2048输出预算，单次调整为
  temperature0.7/top_p0.8/top_k20/presence_penalty1.5。HTTP200，21.278秒，
  finish stop，输出1381/reasoning336，长行最大重复1次；但
  answer_status=invalid_citations、citation_validation=failed、business_passed=false。
  没有重复不等于回答可靠，也不证明后续请求稳定。
- 首次采样对照出现Provider协议错误；当时脚本尚未归档解析失败详情，因此不列为
  成功。随后完善诊断脚本保存协议失败计数，得到上述最终采样报告。
- `native-off-probe.json`：单次原生`/api/v1/chat`请求携带reasoning=off返回400；
  尚未核实具体拒绝原因，不能声称当前模型已关闭推理或服务一定不支持此功能。

## 模型选择与后续

本机模型清单已确认有`qwen3.6-35b-a3b-turboquant-mlx`（majentik，约28.19GB）
与`qwen3.6-27b-oq8-mtp`（csrstka，约30.02GB），本轮查询时两者未加载。
优先以第一个候选对照测试，但名字不含distilled不等于质量、推理开关和JSON能力
已经验证。未下载、加载或切换任何候选模型，未宣称它们通过本项目验收。

下一步需比较同题的完整JSON、逐字引用、主体/日期归属、延迟和重复次数；并补充
证据来源标题与主体识别测试，不能仅更换模型或扩大token预算来掩盖来源混淆。
更换回答模型不需要重建Embedding索引；不要修改Embedding维度。

诊断脚本Ruff、compile及git diff --check通过；现有Provider/摘录/引用定向回归
26 passed。这些是离线契约检查，不是候选模型的真实问答验收。

## 复现

```bash
set -a
source .env
set +a
PYTHON=/Users/liuhui/opt/anaconda3/envs/enterprise-rag/bin/python
"$PYTHON" -m scripts.diagnose_answer_model
# 显式调用当前模型，一次有界请求，不修改线上配置
"$PYTHON" -m scripts.diagnose_answer_model --sample
```

仅在本机同一演示身份和已有文档上复现；诊断拦截的503属于独立ASGI实例，
不是运行中的API故障。采样参数只是本次实验值，不是已核实的官方推荐配置。
