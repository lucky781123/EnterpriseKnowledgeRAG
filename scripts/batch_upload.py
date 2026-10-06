#!/usr/bin/env python3
"""
批量上传文档工具
用法: python scripts/batch_upload.py <directory> [options]
"""

import httpx
import asyncio
import argparse
from pathlib import Path
from typing import List
import json

async def upload_file(
    client: httpx.AsyncClient,
    file_path: Path,
    api_url: str,
    document_id_prefix: str = "doc"
) -> dict:
    """上传单个文件"""

    document_id = f"{document_id_prefix}-{file_path.stem}"
    title = file_path.stem.replace("-", " ").replace("_", " ").title()

    # 读取文件
    file_content = file_path.read_bytes()

    # 确定 MIME 类型
    suffix = file_path.suffix.lower()
    mime_types = {
        '.md': 'text/markdown',
        '.markdown': 'text/markdown',
        '.html': 'text/html',
        '.htm': 'text/html',
        '.pdf': 'application/pdf',
        '.txt': 'text/plain'
    }
    mime_type = mime_types.get(suffix, 'application/octet-stream')

    # 构造请求
    upload_url = f"{api_url}/api/v1/documents"
    files = {
        'file': (file_path.name, file_content, mime_type)
    }
    data = {
        'source_id': f'batch-{document_id}',
        'title': title,
        'idempotency_key': f'idem-{document_id}-{file_path.stat().st_mtime_ns}'
    }

    try:
        response = await client.post(upload_url, files=files, data=data)
        response.raise_for_status()
        result = response.json()

        return {
            "success": True,
            "file": file_path.name,
            "document_id": result['document_id'],
            "version_id": result['version_id']
        }

    except Exception as e:
        return {
            "success": False,
            "file": file_path.name,
            "error": str(e)
        }

async def batch_upload(
    directory: str,
    pattern: str = "*.md",
    api_url: str = "http://127.0.0.1:8000",
    max_concurrent: int = 3,
    timeout: float = 120.0
):
    """批量上传目录中的文档"""

    dir_path = Path(directory)
    if not dir_path.exists() or not dir_path.is_dir():
        raise ValueError(f"目录不存在: {directory}")

    # 查找文件
    files = list(dir_path.glob(pattern))
    if not files:
        print(f"⚠️  未找到匹配的文件: {pattern}")
        return

    print(f"📁 目录: {directory}")
    print(f"📄 找到 {len(files)} 个文件")
    print()

    # 创建信号量控制并发
    semaphore = asyncio.Semaphore(max_concurrent)

    async def upload_with_semaphore(file_path: Path, client: httpx.AsyncClient):
        async with semaphore:
            print(f"📤 上传: {file_path.name}")
            result = await upload_file(client, file_path, api_url)

            if result['success']:
                print(f"✅ {file_path.name} 成功")
            else:
                print(f"❌ {file_path.name} 失败: {result['error']}")

            return result

    # 批量上传
    async with httpx.AsyncClient(timeout=timeout) as client:
        tasks = [upload_with_semaphore(f, client) for f in files]
        results = await asyncio.gather(*tasks)

    # 统计
    success_count = sum(1 for r in results if r['success'])

    print()
    print("=" * 60)
    print("📊 批量上传结果")
    print("=" * 60)
    print(f"总数: {len(results)}")
    print(f"成功: {success_count} ({success_count/len(results)*100:.1f}%)")
    print(f"失败: {len(results) - success_count}")
    print("=" * 60)
    print()

    # 保存结果
    result_file = dir_path / "upload_results.json"
    with open(result_file, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"结果已保存到: {result_file}")

    # 显示成功的文档
    if success_count > 0:
        print()
        print("✅ 成功上传的文档:")
        for r in results:
            if r['success']:
                print(f"   - {r['file']}: {r['document_id']}")

async def main():
    parser = argparse.ArgumentParser(description='批量上传文档到 RAG 系统')
    parser.add_argument('directory', help='文档目录路径')
    parser.add_argument('--pattern', default='*.md', help='文件匹配模式（默认: *.md）')
    parser.add_argument('--api-url', default='http://127.0.0.1:8000', help='API 地址')
    parser.add_argument('--max-concurrent', type=int, default=3, help='最大并发数')
    parser.add_argument('--timeout', type=float, default=120.0, help='超时时间（秒）')

    args = parser.parse_args()

    try:
        await batch_upload(
            directory=args.directory,
            pattern=args.pattern,
            api_url=args.api_url,
            max_concurrent=args.max_concurrent,
            timeout=args.timeout
        )

        return 0

    except Exception as e:
        print(f"❌ 失败: {e}")
        return 1

if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)
