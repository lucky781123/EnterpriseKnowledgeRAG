#!/bin/bash
# 开发环境启动脚本
# 用法: ./scripts/dev_start.sh

set -e

echo "🚀 EnterpriseKnowledgeRAG 开发环境启动"
echo "========================================"

# 检查是否在项目根目录
if [ ! -f "pyproject.toml" ]; then
    echo "❌ 错误: 请在项目根目录运行此脚本"
    exit 1
fi

# 加载环境变量
if [ -f ".env" ]; then
    echo "📝 加载环境变量..."
    set -a
    source .env
    set +a
    echo "✅ 环境变量已加载"
else
    echo "⚠️  警告: .env 文件不存在，使用默认配置"
fi

# 验证关键配置
echo ""
echo "🔍 验证配置..."
if [ -z "$POSTGRES_DSN" ]; then
    echo "❌ POSTGRES_DSN 未设置"
    exit 1
fi
echo "✅ PostgreSQL: 已配置"

if [ -z "$LOCAL_LLM_MODEL" ]; then
    echo "⚠️  LOCAL_LLM_MODEL 未设置，将使用默认值"
fi

# 检查 PostgreSQL
echo ""
echo "🔍 检查 PostgreSQL..."
if docker ps | grep -q enterprise-rag-postgres; then
    echo "✅ PostgreSQL 容器运行中"
else
    echo "⚠️  PostgreSQL 容器未运行"
    echo "   运行: docker start enterprise-rag-postgres"
fi

# 检查 LM Studio（可选）
echo ""
echo "🔍 检查 LM Studio..."
if curl -s -f http://127.0.0.1:1234/v1/models > /dev/null 2>&1; then
    echo "✅ LM Studio 可访问"
else
    echo "⚠️  LM Studio 不可访问 (http://127.0.0.1:1234)"
    echo "   如需测试完整功能，请启动 LM Studio"
fi

# 检查 Ollama（可选）
echo ""
echo "🔍 检查 Ollama..."
if curl -s -f http://127.0.0.1:11434/api/tags > /dev/null 2>&1; then
    echo "✅ Ollama 可访问"
else
    echo "⚠️  Ollama 不可访问 (http://127.0.0.1:11434)"
    echo "   如需测试 Embedding，请启动 Ollama"
fi

# 启动 API
echo ""
echo "========================================"
echo "🚀 启动 FastAPI 开发服务器..."
echo "   地址: http://127.0.0.1:8000"
echo "   文档: http://127.0.0.1:8000/docs"
echo ""
echo "按 Ctrl+C 停止服务器"
echo "========================================"
echo ""

uvicorn enterprise_rag.api.app:app --reload --port 8000
