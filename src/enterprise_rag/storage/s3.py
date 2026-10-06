"""托管 MinIO/S3 adapter；兼容现有 ObjectStorage，不隐式创建 bucket。"""

from __future__ import annotations

import asyncio


class StorageUnavailable(RuntimeError):
    """对外仅暴露安全错误类型，不回显 SDK response、object key 或凭据。"""


def _safe_key(key: str) -> str:
    """保持对象 identity 原样；禁止空路径、反斜杠和目录穿越。"""
    if (
        not isinstance(key, str) or not key or "\\" in key or "\x00" in key
        or any(part in {"", ".", ".."} for part in key.split("/"))
    ):
        raise ValueError("对象 key 非法")
    return key


class S3ObjectStorage:
    """通过线程执行有界 SDK I/O，避免阻塞 FastAPI event loop。"""

    def __init__(self, client, *, bucket: str, prefix: str = "") -> None:
        """接收受维护 SDK client；凭据不保存到业务 metadata。"""
        if not bucket or "/" in bucket:
            raise ValueError("bucket 必须是非空名称")
        if prefix:
            _safe_key(prefix)
        self.client = client
        self.bucket = bucket
        self.prefix = prefix

    @classmethod
    def from_settings(cls, settings):
        """显式创建 path-style S3 client，TLS 默认校验证书且重试有界。"""
        settings.validate()
        if settings.storage_provider != "s3":
            raise ValueError("S3 adapter 尚未启用")
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
            config=Config(
                connect_timeout=5, read_timeout=30, max_pool_connections=16,
                retries={"total_max_attempts": 2, "mode": "standard"},
                s3={"addressing_style": "path"},
            ),
        )
        return cls(client, bucket=settings.s3_bucket, prefix=settings.s3_prefix)

    def _key(self, key: str) -> str:
        """仅给经过校验的业务 key 添加部署级固定前缀。"""
        validated = _safe_key(key)
        return f"{self.prefix}/{validated}" if self.prefix else validated

    async def _call(self, method: str, **kwargs):
        """执行 SDK 方法；原始异常不进入 HTTP 输出或默认日志。"""
        try:
            return await asyncio.to_thread(
                getattr(self.client, method), Bucket=self.bucket, **kwargs,
            )
        except Exception:  # noqa: BLE001 - SDK 异常统一映射为脱敏领域错误。
            raise StorageUnavailable("对象存储不可用") from None

    async def put(self, key: str, data: bytes, content_type: str = "application/octet-stream"):
        """写原文或 manifest；bytes 接口保持兼容，流式上传在下一阶段新增。"""
        await self._call("put_object", Key=self._key(key), Body=data, ContentType=content_type)

    async def get(self, key: str) -> bytes:
        """读取并在同一线程 finally 关闭下载流，避免连接池泄漏。"""
        resolved_key = self._key(key)

        def download() -> bytes:
            """完整关闭 SDK StreamingBody，包含读取失败路径。"""
            response = self.client.get_object(Bucket=self.bucket, Key=resolved_key)
            body = response["Body"]
            try:
                return body.read()
            finally:
                body.close()

        try:
            return await asyncio.to_thread(download)
        except Exception:  # noqa: BLE001 - SDK 异常统一映射为脱敏领域错误。
            raise StorageUnavailable("对象存储不可用") from None

    async def delete(self, key: str) -> None:
        """按 S3 幂等语义删除，版本保留策略由 bucket 运维配置负责。"""
        await self._call("delete_object", Key=self._key(key))

    async def exists(self, key: str) -> bool:
        """只将明确 NoSuchKey/404 判定为缺失，403/5xx 必须失败。"""
        resolved_key = self._key(key)
        try:
            await asyncio.to_thread(
                self.client.head_object, Bucket=self.bucket, Key=resolved_key,
            )
            return True
        except Exception as exc:  # noqa: BLE001 - 需检查 SDK 响应码再映射领域错误。
            response = getattr(exc, "response", {})
            code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
            if code in {"NoSuchKey", "NotFound", "404"}:
                return False
            raise StorageUnavailable("对象存储不可用") from None

    async def health_check(self) -> bool:
        """只读探测 bucket，不写测试对象、不修改权限。"""
        try:
            await self._call("head_bucket")
            return True
        except StorageUnavailable:
            return False

    async def close(self) -> None:
        """释放 SDK 连接池，不清理业务对象。"""
        close = getattr(self.client, "close", None)
        if close:
            await asyncio.to_thread(close)
