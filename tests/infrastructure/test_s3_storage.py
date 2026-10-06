"""S3/MinIO ObjectStorage 契约，使用受控客户端不访问托管服务。"""

import io

import pytest

from enterprise_rag.storage.s3 import S3ObjectStorage, StorageUnavailable


class Client:
    """模拟 bucket 操作与响应 body 生命周期。"""

    def __init__(self):
        """创建测试对象字典。"""
        self.objects = {}
        self.last_body = None

    def put_object(self, **kwargs):
        """保存字节和 content type。"""
        self.objects[kwargs["Key"]] = kwargs["Body"]

    def get_object(self, **kwargs):
        """返回可关闭的下载流。"""
        self.last_body = io.BytesIO(self.objects[kwargs["Key"]])
        return {"Body": self.last_body}

    def head_object(self, **kwargs):
        """只判断对象存在，不返回正文。"""
        if kwargs["Key"] not in self.objects:
            error = RuntimeError("missing")
            error.response = {"Error": {"Code": "NoSuchKey"}}
            raise error
        return {}

    def delete_object(self, **kwargs):
        """幂等删除目标。"""
        self.objects.pop(kwargs["Key"], None)

    def head_bucket(self, **kwargs):
        """返回只读 bucket 探针结果。"""
        return {}


@pytest.mark.asyncio
async def test_minio_round_trip_and_body_close() -> None:
    """保留旧 ObjectStorage 四个方法的行为与安全前缀。"""
    client = Client()
    storage = S3ObjectStorage(client, bucket="rag", prefix="objects")
    await storage.put("tenant/doc/raw.txt", b"hello", "text/plain")
    assert await storage.exists("tenant/doc/raw.txt")
    assert await storage.get("tenant/doc/raw.txt") == b"hello"
    assert client.last_body.closed
    assert await storage.health_check()
    await storage.delete("tenant/doc/raw.txt")
    assert not await storage.exists("tenant/doc/raw.txt")


@pytest.mark.asyncio
async def test_storage_never_normalizes_unsafe_keys() -> None:
    """路径越界、空片段和反斜杠不得成为对象 identity。"""
    storage = S3ObjectStorage(Client(), bucket="rag")
    for key in ("", "/absolute", "../raw", "a/../raw", "a//raw", "a\\raw"):
        with pytest.raises(ValueError):
            await storage.put(key, b"blocked")


@pytest.mark.asyncio
async def test_permission_failure_is_not_treated_as_missing() -> None:
    """认证/5xx 不冒充不存在，错误不回显内部地址或密钥。"""
    class Forbidden(Client):
        """模拟权限拒绝。"""

        def head_object(self, **kwargs):
            """返回包含敏感详情的 SDK 异常。"""
            error = RuntimeError("secret endpoint")
            error.response = {"Error": {"Code": "AccessDenied"}}
            raise error

    with pytest.raises(StorageUnavailable, match="对象存储不可用"):
        await S3ObjectStorage(Forbidden(), bucket="rag").exists("raw")


@pytest.mark.asyncio
async def test_download_failure_still_closes_response_body() -> None:
    """读取流失败也归还 SDK 连接，且不泄露原异常。"""
    class BrokenBody(io.BytesIO):
        """下载中途失败的 SDK 流。"""

        def read(self, *args):
            """模拟私有数据下载中断。"""
            raise OSError("private key and endpoint")

    class BrokenClient(Client):
        """返回需要保证关闭的流。"""

        def get_object(self, **kwargs):
            """创建故障流供生命周期断言。"""
            self.last_body = BrokenBody(b"data")
            return {"Body": self.last_body}

    client = BrokenClient()
    with pytest.raises(StorageUnavailable) as captured:
        await S3ObjectStorage(client, bucket="rag").get("raw")
    assert client.last_body.closed
    assert "private" not in str(captured.value)
