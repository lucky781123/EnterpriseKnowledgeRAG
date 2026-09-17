"""仅供本地 development 环境使用的 HMAC Bearer token。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Iterable
from typing import Any

from .context import RequestContext, SUPPORTED_ROLES


class DevTokenError(ValueError):
    """提供稳定错误类别，不在异常文本中回显 token、secret 或 payload。"""

    def __init__(self, kind: str, safe_message: str) -> None:
        """保存安全错误类别和固定文案，供后续 HTTP 层做稳定映射。"""

        super().__init__(safe_message)
        self.kind = kind
        self.safe_message = safe_message


def _base64url_encode(value: bytes) -> str:
    """将字节编码为无 padding 的 URL-safe Base64 文本。"""

    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    """恢复 URL-safe Base64，并严格拒绝非 ASCII 或非法编码。"""

    padding = "=" * (-len(value) % 4)
    return base64.b64decode(
        f"{value}{padding}".encode("ascii"),
        altchars=b"-_",
        validate=True,
    )


def _canonical_json(value: dict[str, Any]) -> bytes:
    """使用固定 JSON 编码生成可重复签名输入。"""

    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class DevTokenCodec:
    """签发和验证短期开发 token；它不是 OIDC/JWT 的生产替代品。"""

    _HEADER = {"alg": "HS256", "typ": "DEV", "version": 1}
    _CLAIM_NAMES = frozenset({"sub", "tenant_id", "roles", "iat", "exp", "version"})

    def __init__(
        self,
        secret: str,
        *,
        ttl_seconds: int,
        max_clock_skew_seconds: int = 30,
    ) -> None:
        """校验密钥和时间配置，并只在进程内保存签名所需字节。"""

        secret_bytes = secret.encode("utf-8")
        if len(secret_bytes) < 32:
            raise ValueError("DEV_AUTH_SECRET 至少需要 32 字节")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds 必须大于 0")
        if max_clock_skew_seconds < 0:
            raise ValueError("max_clock_skew_seconds 不能小于 0")
        self._secret = secret_bytes
        self._ttl_seconds = ttl_seconds
        self._max_clock_skew_seconds = max_clock_skew_seconds

    def issue(
        self,
        *,
        actor_id: str,
        tenant_id: str,
        roles: Iterable[str],
        now: int | None = None,
    ) -> str:
        """签发包含最小身份 claims 的 token，并对角色做确定性去重排序。"""

        issued_at = int(time.time()) if now is None else now
        normalized_roles = frozenset(role.strip() for role in roles if role.strip())
        # 先构造领域上下文复用同一组严格校验，再把不可变值编码为 payload。
        context = RequestContext(
            actor_id=actor_id.strip(),
            tenant_id=tenant_id.strip(),
            roles=normalized_roles,
            issued_at=issued_at,
            expires_at=issued_at + self._ttl_seconds,
            token_version=1,
        )
        header_segment = _base64url_encode(_canonical_json(self._HEADER))
        payload_segment = _base64url_encode(
            _canonical_json(
                {
                    "sub": context.actor_id,
                    "tenant_id": context.tenant_id,
                    "roles": sorted(context.roles),
                    "iat": context.issued_at,
                    "exp": context.expires_at,
                    "version": context.token_version,
                }
            )
        )
        signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
        signature = hmac.new(self._secret, signing_input, hashlib.sha256).digest()
        return f"{header_segment}.{payload_segment}.{_base64url_encode(signature)}"

    def verify(self, token: str, *, now: int | None = None) -> RequestContext:
        """验证结构、签名、claims 和有效期，成功后返回可信请求上下文。"""

        try:
            header_segment, payload_segment, signature_segment = token.split(".")
        except ValueError as exc:
            raise DevTokenError("invalid_token", "开发身份 token 格式非法") from exc

        signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
        expected_signature = _base64url_encode(
            hmac.new(self._secret, signing_input, hashlib.sha256).digest()
        )
        if not hmac.compare_digest(signature_segment, expected_signature):
            raise DevTokenError("invalid_signature", "开发身份 token 签名无效")

        try:
            header = json.loads(_base64url_decode(header_segment))
            payload = json.loads(_base64url_decode(payload_segment))
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise DevTokenError("invalid_token", "开发身份 token 编码非法") from exc
        if header != self._HEADER or not isinstance(payload, dict):
            raise DevTokenError("invalid_token", "开发身份 token 头或载荷非法")
        if set(payload) != self._CLAIM_NAMES:
            raise DevTokenError("invalid_claims", "开发身份 token claims 不完整")

        try:
            actor_id = payload["sub"]
            tenant_id = payload["tenant_id"]
            roles = payload["roles"]
            issued_at = payload["iat"]
            expires_at = payload["exp"]
            token_version = payload["version"]
            if not isinstance(actor_id, str) or not isinstance(tenant_id, str):
                raise ValueError("身份 claim 类型非法")
            if not isinstance(roles, list) or any(not isinstance(role, str) for role in roles):
                raise ValueError("roles claim 类型非法")
            if not isinstance(issued_at, int) or not isinstance(expires_at, int):
                raise ValueError("时间 claim 类型非法")
            if not isinstance(token_version, int):
                raise ValueError("version claim 类型非法")
            context = RequestContext(
                actor_id=actor_id,
                tenant_id=tenant_id,
                roles=frozenset(roles),
                issued_at=issued_at,
                expires_at=expires_at,
                token_version=token_version,
            )
        except ValueError as exc:
            raise DevTokenError("invalid_claims", "开发身份 token claims 非法") from exc

        current_time = int(time.time()) if now is None else now
        if current_time >= context.expires_at:
            raise DevTokenError("expired_token", "开发身份 token 已过期")
        if context.issued_at > current_time + self._max_clock_skew_seconds:
            raise DevTokenError("invalid_claims", "开发身份 token 签发时间非法")
        if context.roles - SUPPORTED_ROLES:
            # RequestContext 已做同一检查；保留显式分支说明 token 验证不接受未知角色。
            raise DevTokenError("invalid_claims", "开发身份 token roles 非法")
        return context


__all__ = ["DevTokenCodec", "DevTokenError"]
