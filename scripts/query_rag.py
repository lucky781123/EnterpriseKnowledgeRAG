#!/usr/bin/env python3
"""
RAG 查询工具
用法: python scripts/query_rag.py "你的问题" [options]
"""

import httpx
import asyncio
import argparse
import json
from typing import Optional, List

async def query_rag(
    question: str,
    evidence: Optional[List[dict]] = None,
    model_alias: str = "local_answer",
    temperature: float = 0.7,
    max_tokens: int = 512,
    api_url: str = "http://127.0.0.1:8000",
    stream: bool = False,
    timeout: float = 60.0
) -> dict:
    """向 RAG 系统提问"""

    # 如果没有提供证据，使用演示证据
    if evidence is None:
        evidence = [
            {
                "evidence_id": "demo-1",
                "text": "这是一个演示证据。实际使用时应该从检索系统获取相关文档片段。",
                "source": "demo#1"
            }
        ]

    request_data = {
        "question": question,
        "evidence": evidence,
        "model_alias": model_alias,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "timeout_ms": int(timeout * 1000)
    }

    print(f"🤔 提问: {question}")
    print()

    if stream:
        return await query_rag_stream(request_data, api_url, timeout)
    else:
        return await query_rag_sync(request_data, api_url, timeout)

async def query_rag_sync(request_data: dict, api_url: str, timeout: float) -> dict:
    """同步查询"""

    query_url = f"{api_url}/api/v1/query"

    async with httpx.AsyncClient(timeout=timeout + 5) as client:
        try:
            print("⏳ 生成中...")
            response = await client.post(query_url, json=request_data)
            response.raise_for_status()

            result = response.json()

            print("✅ 回答:")
            print()
            print(result['message'])
            print()

            if result.get('citations'):
                print("📚 引用:")
                for i, citation in enumerate(result['citations'], 1):
                    print(f"   [{i}] {citation.get('quote', 'N/A')}")
                    if citation.get('reason'):
                        print(f"       理由: {citation['reason']}")
                print()

            print("📊 统计:")
            print(f"   状态: {result['answer_status']}")
            print(f"   引用校验: {result['citation_validation']}")

            timings = result.get('timings', {})
            print(f"   队列: {timings.get('queue_ms', 0):.0f}ms")
            print(f"   生成: {timings.get('generation_ms', 0):.0f}ms")
            print(f"   总计: {timings.get('completion_ms', 0):.0f}ms")
            print()

            return result

        except httpx.HTTPStatusError as e:
            print(f"❌ 查询失败: HTTP {e.response.status_code}")
            print(f"   错误详情: {e.response.text}")
            raise
        except httpx.RequestError as e:
            print(f"❌ 请求失败: {e}")
            print("   请检查 API 服务是否运行: http://127.0.0.1:8000")
            raise

async def query_rag_stream(request_data: dict, api_url: str, timeout: float) -> dict:
    """流式查询"""

    query_url = f"{api_url}/api/v1/query/stream"

    async with httpx.AsyncClient(timeout=timeout + 5) as client:
        try:
            print("💬 回答: ", end="", flush=True)

            full_message = ""
            citations = []
            result_data = {}

            async with client.stream("POST", query_url, json=request_data) as response:
                response.raise_for_status()

                async for line in response.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue

                    try:
                        data = json.loads(line[6:])  # 去掉 "data: " 前缀
                    except json.JSONDecodeError:
                        continue

                    if data["type"] == "token":
                        # 打印生成的 token
                        text = data.get("text", "")
                        print(text, end="", flush=True)
                        full_message += text

                    elif data["type"] == "citation":
                        # 记录引用
                        citation = data["citation"]
                        citations.append(citation)

                    elif data["type"] == "done":
                        # 完成
                        result_data = data
                        break

            print()
            print()

            if citations:
                print("📚 引用:")
                for i, citation in enumerate(citations, 1):
                    print(f"   [{i}] {citation.get('quote', 'N/A')}")
                    if citation.get('reason'):
                        print(f"       理由: {citation['reason']}")
                print()

            print("📊 统计:")
            timings = result_data.get('timings', {})
            print(f"   生成: {timings.get('generation_ms', 0):.0f}ms")
            print(f"   总计: {timings.get('completion_ms', 0):.0f}ms")
            print()

            return {
                "message": full_message,
                "citations": citations,
                **result_data
            }

        except httpx.HTTPStatusError as e:
            print(f"\n❌ 查询失败: HTTP {e.response.status_code}")
            print(f"   错误详情: {e.response.text}")
            raise
        except httpx.RequestError as e:
            print(f"\n❌ 请求失败: {e}")
            raise

async def main():
    parser = argparse.ArgumentParser(description='向 RAG 系统提问')
    parser.add_argument('question', help='你的问题')
    parser.add_argument('--evidence', help='证据 JSON 文件路径（可选）')
    parser.add_argument('--model', default='local_answer', help='模型别名')
    parser.add_argument('--temperature', type=float, default=0.7, help='温度参数')
    parser.add_argument('--max-tokens', type=int, default=512, help='最大 token 数')
    parser.add_argument('--api-url', default='http://127.0.0.1:8000', help='API 地址')
    parser.add_argument('--stream', action='store_true', help='使用流式响应')
    parser.add_argument('--timeout', type=float, default=60.0, help='超时时间（秒）')

    args = parser.parse_args()

    # 加载证据（如果提供）
    evidence = None
    if args.evidence:
        with open(args.evidence, 'r', encoding='utf-8') as f:
            evidence = json.load(f)

    try:
        await query_rag(
            question=args.question,
            evidence=evidence,
            model_alias=args.model,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            api_url=args.api_url,
            stream=args.stream,
            timeout=args.timeout
        )

        return 0

    except Exception as e:
        print(f"❌ 失败: {e}")
        return 1

if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)
