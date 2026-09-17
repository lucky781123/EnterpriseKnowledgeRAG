from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from enterprise_rag.security.context import RequestContext
from enterprise_rag.security.dev_token import DevTokenCodec, DevTokenError


SECRET = "0123456789abcdef0123456789abcdef"


def test_dev_token_round_trip_builds_immutable_request_context() -> None:
    """有效 token 应恢复可信且不可变的 tenant、actor 和角色上下文。"""

    codec = DevTokenCodec(SECRET, ttl_seconds=3600)

    token = codec.issue(
        actor_id="user-alice",
        tenant_id="tenant-a",
        roles=["editor", "reader", "reader"],
        now=1_789_600_000,
    )
    context = codec.verify(token, now=1_789_600_100)

    assert token.count(".") == 2
    assert SECRET not in token
    assert context == RequestContext(
        actor_id="user-alice",
        tenant_id="tenant-a",
        roles=frozenset({"editor", "reader"}),
        issued_at=1_789_600_000,
        expires_at=1_789_603_600,
        token_version=1,
    )
    with pytest.raises(FrozenInstanceError):
        context.tenant_id = "tenant-b"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("token_mutator", "expected_kind"),
    [
        (lambda token: f"{token[:-1]}{'A' if token[-1] != 'A' else 'B'}", "invalid_signature"),
        (lambda _token: "missing-segments", "invalid_token"),
    ],
)
def test_dev_token_rejects_tampered_or_malformed_value(token_mutator, expected_kind) -> None:
    """篡改签名或破坏 token 结构时必须 fail closed。"""

    codec = DevTokenCodec(SECRET, ttl_seconds=3600)
    token = codec.issue(
        actor_id="user-alice",
        tenant_id="tenant-a",
        roles=["reader"],
        now=1_789_600_000,
    )

    with pytest.raises(DevTokenError) as exc_info:
        codec.verify(token_mutator(token), now=1_789_600_100)

    assert exc_info.value.kind == expected_kind


def test_dev_token_rejects_expired_or_future_issued_token() -> None:
    """过期 token 和明显来自未来的 token 都不能建立请求上下文。"""

    codec = DevTokenCodec(SECRET, ttl_seconds=60, max_clock_skew_seconds=5)
    token = codec.issue(
        actor_id="user-alice",
        tenant_id="tenant-a",
        roles=["reader"],
        now=1_789_600_000,
    )

    with pytest.raises(DevTokenError) as expired:
        codec.verify(token, now=1_789_600_061)
    assert expired.value.kind == "expired_token"

    with pytest.raises(DevTokenError) as future:
        codec.verify(token, now=1_789_599_990)
    assert future.value.kind == "invalid_claims"


@pytest.mark.parametrize(
    ("actor_id", "tenant_id", "roles"),
    [
        ("", "tenant-a", ["reader"]),
        ("user-alice", "", ["reader"]),
        ("user-alice", "tenant-a", []),
        ("user-alice", "tenant-a", ["owner"]),
    ],
)
def test_dev_token_rejects_invalid_issue_claims(actor_id, tenant_id, roles) -> None:
    """签发入口应拒绝空身份、空租户、空角色和未支持角色。"""

    codec = DevTokenCodec(SECRET, ttl_seconds=3600)

    with pytest.raises(ValueError):
        codec.issue(
            actor_id=actor_id,
            tenant_id=tenant_id,
            roles=roles,
            now=1_789_600_000,
        )


def test_dev_token_requires_a_32_byte_secret() -> None:
    """开发 token 也必须使用足够长的 secret，避免弱密钥被误当成安全配置。"""

    with pytest.raises(ValueError, match="32"):
        DevTokenCodec("too-short", ttl_seconds=3600)
