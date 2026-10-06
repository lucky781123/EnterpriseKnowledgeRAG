#!/bin/bash
# Migration 运行脚本
# 用法: ./scripts/run_migration.sh

set -e

echo "🔧 EnterpriseKnowledgeRAG Migration"
echo "===================================="

# 检查是否在项目根目录
if [ ! -f "pyproject.toml" ]; then
    echo "❌ 错误: 请在项目根目录运行此脚本"
    exit 1
fi

# 加载环境变量
if [ ! -f ".env" ]; then
    echo "❌ 错误: .env 文件不存在"
    echo "   请先运行: cp .env.example .env"
    echo "   并填写必需的配置"
    exit 1
fi

echo "📝 加载环境变量..."
set -a
source .env
set +a

# 验证关键配置
if [ -z "$POSTGRES_DSN" ]; then
    echo "❌ 错误: POSTGRES_DSN 未设置"
    echo "   请在 .env 中配置 PostgreSQL 连接串"
    exit 1
fi

echo "✅ 环境变量已加载"
echo "   PostgreSQL: 已配置（连接串不输出）"

# 检查 PostgreSQL 连接
echo ""
echo "🔍 检查 PostgreSQL 连接..."
if docker ps | grep -q enterprise-rag-postgres; then
    echo "✅ PostgreSQL 容器运行中"
else
    echo "⚠️  PostgreSQL 容器未运行"
    echo "   正在启动容器..."
    docker start enterprise-rag-postgres || {
        echo "❌ 容器启动失败"
        echo "   如果容器不存在，请参考 SETUP_GUIDE.md 创建容器"
        exit 1
    }
    sleep 2
fi

# 运行 migration
echo ""
echo "🚀 运行数据库 migration..."
python -m enterprise_rag.db.migration_cli

EXIT_CODE=$?

echo ""
echo "===================================="
if [ $EXIT_CODE -eq 0 ]; then
    echo "✅ Migration 完成"
else
    echo "❌ Migration 失败（退出码: $EXIT_CODE）"
fi
echo "===================================="

exit $EXIT_CODE
