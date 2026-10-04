"""无文档数量上限的Gradio兼容临时上传路由；沿用磁盘缓存和安全文件名。"""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request


def mount_unlimited_upload(app: Any, blocks: Any, *, path: str) -> None:
    """在Gradio子应用前注册上传路由，保留主应用的本机peer/Origin保护。"""
    from gradio import processing_utils
    from gradio.route_utils import GradioMultiPartParser, GradioUploadFile, MultiPartException
    from gradio.routes import file_upload_statuses
    from gradio_client.utils import strip_invalid_filename_characters
    from python_multipart.multipart import parse_options_header

    upload_dir = Path(processing_utils.get_upload_folder())

    @app.post(f"{path.rstrip('/')}/gradio_api/upload", include_in_schema=False)
    async def upload_files(request: Request, upload_id: str | None = None) -> list[str]:
        """流式接收任意数量文件并落磁盘；不把文件正文保存在Python列表中。"""
        content_type, _ = parse_options_header(request.headers.get("content-type", ""))
        if content_type != b"multipart/form-data":
            raise HTTPException(status_code=400, detail="上传必须使用multipart/form-data")
        upload_dir.mkdir(parents=True, exist_ok=True)
        if upload_id:
            file_upload_statuses.track(upload_id)
        parser = GradioMultiPartParser(
            request.headers, request.stream(), upload_dir=upload_dir,
            max_files=math.inf, max_file_size=math.inf,
            upload_id=upload_id, upload_progress=file_upload_statuses if upload_id else None,
        )
        try:
            form = await parser.parse()
        except MultiPartException as exc:
            raise HTTPException(status_code=400, detail="上传multipart格式无效") from exc
        output = []
        for item in form.getlist("files"):
            if not isinstance(item, GradioUploadFile):
                raise HTTPException(status_code=400, detail="上传项必须是文件")
            # 文件名先取basename再剔除不安全字符；hash目录保持Gradio原缓存语义。
            name = strip_invalid_filename_characters(Path(item.filename or "upload").name)
            directory = upload_dir / item.sha.hexdigest()
            directory.mkdir(exist_ok=True)
            destination = directory / (name or "upload")
            item.file.close()
            os.replace(item.file.name, destination)
            output.append(str(destination))
        # Gradio只允许处理本服务已登记的上传路径，不能接受任意本地文件。
        blocks.upload_file_set.update(output)
        return output
