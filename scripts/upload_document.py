#!/usr/bin/env python3
"""
文档上传工具
用法: python scripts/upload_document.py <file_path> [options]
"""

import httpx
import asyncio
import argparse
import json
from pathlib import Path
from typing import Optional

async def upload_document(
    file_path: str,
    document_id: Optional[str] = None,
    title: Optional[str] = None,
    api_url: str = "http://127.0.0.1:8000",
    timeout: float = 120.0
) -> dict:
    """上传文档到 RAG 系统"""

    file_path_obj = Path(file_path)

    if not file_path_obj.exists():
        raise FileNotFoundError(f"文件不存在: {file_path}")

    # 自动生成 document_id 和 title
    if document_id is None:
        document_id = f"doc-{file_path_obj.stem}"

    if title is None:
        title = file_path_obj.stem.replace("-", " ").replace("_", " ").title()

    # 读取文件
    file_content = file_path_obj.read_bytes()
    file_name = file_path_obj.name

    # 确定 MIME 类型
    suffix = file_path_obj.suffix.lower()
    mime_types = {
        '.md': 'text/markdown',
        '.markdown': 'text/markdown',
        '.html': 'text/html',
        '.htm': 'text/html',
        '.pdf': 'application/pdf',
        '.txt': 'text/plain'
    }
    mime_type = mime_types.get(suffix, 'application/octet-stream')

    print(f"📤 上传文档")
    print(f"   文件: {file_name}")
    print(f"   大小: {len(file_content) / 1024:.1f} KB")
    print(f"   类型: {mime_type}")
    print(f"   标题: {title}")
    print(f"   文档ID: {document_id}")
    print()

    # 构造请求
    upload_url = f"{api_url}/api/v1/documents"

    files = {
        'file': (file_name, file_content, mime_type)
    }
    data = {
        'source_id': f'upload-{document_id}',
        'title': title,
        'idempotency_key': f'idem-{document_id}-{file_path_obj.stat().st_mtime_ns}'
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            print("⏳ 上传中...")
            response = await client.post(upload_url, files=files, data=data)
            response.raise_for_status()

            result = response.json()

            print("✅ 上传成功！")
            print(f"   Document ID: {result.get('document_id')}")
            print(f"   Version ID: {result.get('version_id')}")
            print(f"   Job ID: {result.get('job_id')}")
            print()

            # 等待索引完成
            print("⏳ 等待索引完成...")
            await asyncio.sleep(2)

            # 检查状态
            status_url = f"{api_url}/api/v1/documents/{result['document_id']}/status"
            status_response = await client.get(status_url)

            if status_response.status_code == 200:
                status = status_response.json()
                print("📊 文档状态:")
                print(f"   状态: {status.get('document_status')}")
                if 'current_version' in status:
                    version = status['current_version']
                    print(f"   版本: {version.get('version_id')}")
                    print(f"   状态: {version.get('status')}")
                if 'generation' in status:
                    gen = status['generation']
                    print(f"   索引状态: {gen.get('status')}")
                    print(f"   块数量: {gen.get('actual_item_count')}/{gen.get('expected_item_count')}")
                print()

            return result

        except httpx.HTTPStatusError as e:
            print(f"❌ 上传失败: HTTP {e.response.status_code}")
            print(f"   错误详情: {e.response.text}")
            raise
        except httpx.RequestError as e:
            print(f"❌ 请求失败: {e}")
            print("   请检查 API 服务是否运行: http://127.0.0.1:8000")
            raise

async def main():
    parser = argparse.ArgumentParser(description='上传文档到 RAG 系统')
    parser.add_argument('file_path', help='文档文件路径')
    parser.add_argument('--document-id', help='文档ID（默认从文件名生成）')
    parser.add_argument('--title', help='文档标题（默认从文件名生成）')
    parser.add_argument('--api-url', default='http://127.0.0.1:8000', help='API 地址')
    parser.add_argument('--timeout', type=float, default=120.0, help='超时时间（秒）')

    args = parser.parse_args()

    try:
        result = await upload_document(
            file_path=args.file_path,
            document_id=args.document_id,
            title=args.title,
            api_url=args.api_url,
            timeout=args.timeout
        )

        print("✅ 完成！")
        print()
        print("下一步:")
        print(f"  1. 查询文档: curl {args.api_url}/api/v1/documents/{result['document_id']}/status")
        print(f"  2. 测试检索: python scripts/query_rag.py '你的问题'")

    except Exception as e:
        print(f"❌ 失败: {e}")
        return 1

    return 0

if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)
