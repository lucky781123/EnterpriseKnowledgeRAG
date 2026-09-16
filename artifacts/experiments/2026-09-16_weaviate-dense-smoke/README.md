# Weaviate Dense Smoke

本实验产物只记录 2026-09-16 的 Weaviate Dense transport smoke 状态。

- `metrics.json` 是脱敏报告，固定记录 tenant 独立 collection、generation scope 和各阶段状态。
- 本次运行使用默认 `WEAVIATE_ENABLED=false`，因此真实 Weaviate 访问被明确标记为 `skipped`。
- 没有读取、写出或提交 DSN、密码、API key、文档正文或完整模型回答。
- transport contract、SQLite generation/publish/rollback/cleanup 契约和 API 显式注入测试属于离线证据，不等同于真实 Weaviate smoke。
