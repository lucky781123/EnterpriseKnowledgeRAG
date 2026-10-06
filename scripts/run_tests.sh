#!/bin/bash
# 测试运行脚本
# 用法: ./scripts/run_tests.sh [options]
#   ./scripts/run_tests.sh          # 运行离线测试
#   ./scripts/run_tests.sh --all    # 运行所有测试（包括集成测试）
#   ./scripts/run_tests.sh --quick  # 快速测试（只运行标记为 quick 的测试）

set -e

echo "🧪 EnterpriseKnowledgeRAG 测试运行"
echo "===================================="

# 检查是否在项目根目录
if [ ! -f "pyproject.toml" ]; then
    echo "❌ 错误: 请在项目根目录运行此脚本"
    exit 1
fi

# 加载环境变量
if [ -f ".env" ]; then
    set -a
    source .env
    set +a
fi

# 解析参数
TEST_MODE="offline"
if [ "$1" == "--all" ]; then
    TEST_MODE="all"
elif [ "$1" == "--quick" ]; then
    TEST_MODE="quick"
fi

echo "测试模式: $TEST_MODE"
echo ""

case $TEST_MODE in
    "offline")
        echo "🔄 运行离线测试（不需要外部服务）..."
        pytest tests/ -v -m "not integration" --tb=short
        ;;
    "all")
        echo "🔄 运行所有测试（包括集成测试）..."
        if [ -z "$POSTGRES_DSN" ]; then
            echo "⚠️  警告: POSTGRES_DSN 未设置，集成测试将被跳过"
        fi
        pytest tests/ -v --tb=short
        ;;
    "quick")
        echo "🔄 运行快速测试..."
        pytest tests/ -v -m "quick" --tb=short
        ;;
esac

EXIT_CODE=$?

echo ""
echo "===================================="
if [ $EXIT_CODE -eq 0 ]; then
    echo "✅ 测试通过"
else
    echo "❌ 测试失败（退出码: $EXIT_CODE）"
fi
echo "===================================="

exit $EXIT_CODE
