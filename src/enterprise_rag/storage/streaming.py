"""上传先流式落临时磁盘，避免 API 把大文件一次读入内存。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import tempfile
from pathlib import Path

from enterprise_rag.storage.local import LocalObjectStorage
from enterprise_rag.storage.s3 import S3ObjectStorage


async def persist_upload(storage, upload, key: str, *, root: Path, max_bytes: int):
    """有界流式读取 UploadFile；SDK 上传完成前不删除临时文件。"""
    root.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=root, suffix=".upload")
    path = Path(name)
    digest, size = hashlib.sha256(), 0
    try:
        with os.fdopen(descriptor, "wb") as output:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("异步上传超过受理大小上限")
                digest.update(chunk)
                await asyncio.to_thread(output.write, chunk)
        if size == 0:
            raise ValueError("异步上传不能为空")

        def transfer():
            """只使用服务器生成的 key，原子替换本地对象或 SDK file upload。"""
            if isinstance(storage, LocalObjectStorage):
                destination = storage._path(key)
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_name(destination.name + ".partial-" + path.stem)
                try:
                    shutil.copyfile(path, temporary)
                    os.replace(temporary, destination)
                finally:
                    temporary.unlink(missing_ok=True)
            elif isinstance(storage, S3ObjectStorage):
                # 客户端禁用额外 SDK 线程，避免取消后后台 multipart 使用已删除文件。
                from boto3.s3.transfer import TransferConfig

                storage.client.upload_file(
                    str(path), storage.bucket, storage._key(key),
                    Config=TransferConfig(use_threads=False),
                )
            else:
                raise TypeError("异步受理需要支持流式文件上传的 storage adapter")

        transfer_task = asyncio.create_task(asyncio.to_thread(transfer))
        try:
            await asyncio.shield(transfer_task)
        except asyncio.CancelledError:
            # 线程不能强制取消；等待资源收尾后再传播取消，不提前删文件。
            await transfer_task
            raise
        return digest.hexdigest(), size
    finally:
        path.unlink(missing_ok=True)
