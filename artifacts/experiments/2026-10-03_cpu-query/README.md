# CPU排查与真实条文查询

日期：2026-10-03。当前报告不保存私人文档、回答、引用正文、token或模型reasoning。

## 查询前与停API对照

18:26 top连续采样：WindowServer66.2/73.6%，Docker VM23.3/44.3%，
系统idle82.78/79.94%。受控API19808约4.9%，Ollama runner约0.3%。
API线程sample中11个OpenMP worker均在cond_wait，没有持续模型计算证据。

18:44仅停止受控API后：WindowServer69.1/67.6%，Docker VM24.5/24.0%，
系统idle83.89/82.54%。RAG Weaviate2.35%，PostgreSQL0%。
这支持当前持续WindowServer负载不依赖RAG API的结论，不能因此认定桌面/虚拟机
存在死循环，亦未对共享Docker、其他项目容器、桌面会话做破坏性重启。

原始本机采样（临时诊断文件，不是长期归档）：
`/tmp/rag-api-cpu-current.sample.txt`、`/tmp/rag-docker-vm-cpu.sample.txt`。
WindowServer栈采样权限不足，不使用sudo或kill；具体绘制来源尚未定位。

## 修复与复现

BGE默认CPU4线程；单模型串行准入，取消后保留占位直到native batch结束，
Event在批次边界停止后续计算。当前native kernel不能被Python强制中断。
产品/UI默认2048tokens与300秒，HTTP client310秒；
incomplete/无效引用不再显示passed。默认较大输出预算并不保证模型一定完整回答。

```bash
bash start.sh
/Users/liuhui/opt/anaconda3/envs/enterprise-rag/bin/python -m scripts.verify_cpu_runtime --query --label real-query
```

此脚本仅用于本机显式匿名模式，真实查询已入库的人事文档；
`real-query.json`保存HTTP/业务状态、retrieval trace、耗时、usage和CPU，
不保存任何私人正文。只有answered且citation_validation=passed才业务通过。
进程pcpu为ps的估计值；需要结合累计cpu_time增量与间隔解释，不作为精确瞬时profiling。
CPU结论仅适用当前观测，不能代替容量、热功耗或生产性能测试。

## 后续任务边界

用户已要求停止继续排查WindowServer/Docker风扇问题；该方向不再由本项目任务处理。
