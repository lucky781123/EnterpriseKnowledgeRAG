"""F2-G 有界 benchmark 的确定性统计入口。

本文件先定义与 Provider 无关的样本和统计契约，再由 Fake 或真实本地 Provider
填充样本。这样可以明确区分“统计代码正确”与“某个模型在某台机器上的观测”，
也避免把 Fake 的极快延迟误写成真实模型容量。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import inspect
import json
import math
import os
import platform
import subprocess
import sys
import statistics
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from enterprise_rag.excerpts.protocols import EvidenceInput
from enterprise_rag.excerpts.service import (
    ExcerptAnswerService,
    VisibleStreamSanitizer,
    build_excerpt_prompt,
)
from enterprise_rag.providers.errors import ProviderError, ProviderErrorKind
from enterprise_rag.providers.fake import FakeChatProvider
from enterprise_rag.providers.lmstudio import LMStudioChatProvider
from enterprise_rag.providers.protocols import ChatRequest, ChatResponse, ChatStreamEvent, ChatUsage
from enterprise_rag.providers.scheduler import LocalLLMScheduler, SchedulerLease


@dataclass(frozen=True, slots=True)
class BenchmarkSample:
    """一条 benchmark 观测值。

    ``outcome`` 只允许 ``success``、``failure`` 和 ``cancelled`` 三类。成功延迟
    统计只读取 ``success`` 样本，因此错误、截断和主动取消不会悄悄混进 P50/P95。
    可选字段使用 ``None`` 表示该阶段没有发生，不能用 0 伪造未到达的阶段。
    """

    mode: str
    index: int
    outcome: str
    completion_ms: float
    queue_ms: float | None
    ttft_ms: float | None
    generation_ms: float | None
    error_kind: str | None = None
    finish_reason: str | None = None
    answer_status: str | None = None
    citation_validation: str | None = None
    cleanup_ms: float | None = None
    visible_chars: int = 0
    chunk_count: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cancellation_requested: bool = False
    cancellation_observed: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        """返回可直接写入 JSON 的普通字典。"""

        return asdict(self)


# 这个 fixture 只用于验证生成侧契约；它不是检索结果，也没有 ACL 或来源
# 真实性含义。所有真实模型组都复用同一份输入，便于比较协议和时延而不是
# 比较不同上下文带来的答案差异。
_BENCHMARK_QUESTION = "证据是什么？"
_BENCHMARK_EVIDENCE = (
    EvidenceInput(
        evidence_id="doc-1",
        text="确定性证据",
        source="F2-G benchmark fixture",
    ),
)
_DEFAULT_CONCURRENCY_LEVELS = (1, 2)
_DEFAULT_GROUP_TIMEOUT_SECONDS = 600.0
_DEFAULT_CANCEL_CLEANUP_TIMEOUT_SECONDS = 10.0
_DEFAULT_CANCEL_TRIGGER_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class _StreamObservation:
    """内部流消费结果；不包含原始模型正文，避免 benchmark 产物泄露证据。"""

    visible_text: str
    finish_reason: str | None
    usage: ChatUsage | None
    ttft_ms: float | None
    chunk_count: int
    saw_terminal_event: bool


def _utc_timestamp() -> str:
    """生成带时区的 UTC 时间，避免不同机器的本地时区造成产物歧义。"""

    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(value: bytes) -> str:
    """返回稳定的 SHA-256 十六进制摘要。"""

    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    """对 JSON 可表达对象做规范化哈希，确保键顺序不影响结果。"""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def _normalized_api_base(base_url: str) -> str:
    """将 LM Studio 根地址归一化为带单个 `/v1` 的形式。"""

    normalized = base_url.rstrip("/")
    return normalized if normalized.endswith("/v1") else f"{normalized}/v1"


def _git_output(*args: str, cwd: Path) -> str | None:
    """读取只读 Git 元数据；失败时返回 ``None`` 而不阻断 benchmark。"""

    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _git_dirty_patch_hash(cwd: Path) -> str | None:
    """对已跟踪的工作树/暂存区 patch 做摘要，不读取或打印密钥。

    `.env` 默认被忽略且不会出现在普通 diff 中；这里仍只保存摘要，不把 patch
    正文写入实验产物。未跟踪文件不纳入 patch 摘要，避免把大文件或本地密钥
    误打包；它们是否存在会在 `git_status` 字段中单独记录。
    """

    try:
        working = subprocess.run(
            ["git", "diff", "--binary"],
            cwd=cwd,
            check=False,
            capture_output=True,
        )
        staged = subprocess.run(
            ["git", "diff", "--cached", "--binary"],
            cwd=cwd,
            check=False,
            capture_output=True,
        )
    except OSError:
        return None
    if working.returncode != 0 or staged.returncode != 0:
        return None
    return _sha256_bytes(working.stdout + b"\0" + staged.stdout)


def _package_version(name: str) -> str | None:
    """获取运行时依赖版本；缺失时保留 ``None`` 而不是猜测。"""

    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _fixture_hash() -> str:
    """计算本次提交给模型的 question/evidence 数据摘要。"""

    return _sha256_json(
        {
            "question": _BENCHMARK_QUESTION,
            "evidence": [asdict(item) for item in _BENCHMARK_EVIDENCE],
        }
    )


def _prompt_hash(model: str, max_tokens: int) -> str:
    """计算实际 Prompt（含模型请求参数）的摘要。"""

    messages = build_excerpt_prompt(_BENCHMARK_QUESTION, _BENCHMARK_EVIDENCE)
    return _sha256_json(
        {
            "messages": [asdict(message) for message in messages],
            "model": model,
            "max_tokens": max_tokens,
            "temperature": 0.0,
        }
    )


def _registry_file_hash() -> str | None:
    """记录模型注册表版本，便于复现实验配置而不读取 `.env`。"""

    path = Path(__file__).resolve().parents[1] / "config" / "model-registry.yaml"
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError:
        return None


def _build_metadata(
    *,
    base_url: str,
    model: str,
    max_tokens: int,
    concurrency_levels: Sequence[int],
    iterations: int,
    warmup: int,
    cancellation_runs: int,
    group_timeout_seconds: float,
    started_at: str,
    finished_at: str,
) -> dict[str, Any]:
    """生成实验元数据；只记录配置摘要，不记录 API key 或原始正文。"""

    repository = Path(__file__).resolve().parents[1]
    benchmark_config = {
        "base_url": _normalized_api_base(base_url),
        "model": model,
        "max_tokens": max_tokens,
        "concurrency_levels": list(concurrency_levels),
        "iterations": iterations,
        "warmup_per_mode": warmup,
        "cancellation_runs": cancellation_runs,
        "group_timeout_seconds": group_timeout_seconds,
        "temperature": 0.0,
    }
    return {
        "started_at": started_at,
        "finished_at": finished_at,
        "command": list(sys.argv),
        "code_commit": _git_output("rev-parse", "HEAD", cwd=repository) or "unknown",
        "dirty_patch_hash": _git_dirty_patch_hash(repository) or "unavailable",
        "git_status": _git_output("status", "--short", cwd=repository) or "clean",
        "config_hash": _sha256_json(benchmark_config),
        "config": benchmark_config,
        "fixture_hash": _fixture_hash(),
        "prompt_hash": _prompt_hash(model, max_tokens),
        "model_registry_hash": _registry_file_hash() or "unavailable",
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "httpx": _package_version("httpx"),
            "fastapi": _package_version("fastapi"),
            "pydantic": _package_version("pydantic"),
        },
    }


def _nearest_rank(values: list[float], percentile: float) -> float | None:
    """按固定 nearest-rank 算法计算分位数。

    rank 使用 ``ceil(p * n)`` 的 1-based 定义。比如两个样本的 P50 是第
    1 个值、P95 是第 2 个值；这正是小样本 benchmark 需要的可复现规则，
    不依赖 Python/NumPy 的插值实现差异。
    """

    if not values:
        return None
    if not 0 < percentile <= 1:
        raise ValueError("percentile 必须位于 (0, 1] 区间")
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def _numeric_summary(values: list[float]) -> dict[str, Any]:
    """为一个阶段耗时生成统一摘要；空阶段保留 null 而不是伪造数字。"""

    ordered = sorted(values)
    return {
        "raw": ordered,
        "count": len(ordered),
        "p50_nearest_rank": _nearest_rank(ordered, 0.50),
        "p95_nearest_rank": _nearest_rank(ordered, 0.95),
        "mean": statistics.mean(ordered) if ordered else None,
    }


def summarize_samples(samples: list[BenchmarkSample]) -> dict[str, Any]:
    """汇总样本分类、延迟分位数和错误分布。

    这里的 ``success`` 表示调用完成且应用层结果通过本次 benchmark 定义的
    成功条件；它不是事实正确性、检索召回率或生产容量证明。错误率和取消率
    使用全部样本作为分母，避免只保留成功请求造成乐观偏差。
    """

    success = [sample for sample in samples if sample.outcome == "success"]
    failures = [sample for sample in samples if sample.outcome == "failure"]
    cancelled = [sample for sample in samples if sample.outcome == "cancelled"]

    error_counts: dict[str, int] = {}
    for sample in failures:
        kind = sample.error_kind or "unknown"
        error_counts[kind] = error_counts.get(kind, 0) + 1

    def phase_values(name: str) -> list[float]:
        """只收集实际到达该阶段的非空数值。"""

        return [value for sample in samples if (value := getattr(sample, name)) is not None]

    sample_count = len(samples)
    denominator = sample_count or 1
    completion_values = [sample.completion_ms for sample in success]
    summary: dict[str, Any] = {
        "sample_count": sample_count,
        "success_count": len(success),
        "failure_count": len(failures),
        "cancelled_count": len(cancelled),
        "success_rate": len(success) / denominator,
        "failure_rate": len(failures) / denominator,
        "cancelled_rate": len(cancelled) / denominator,
        "success_latency_ms": _numeric_summary(completion_values),
        "queue_latency_ms": _numeric_summary(phase_values("queue_ms")),
        "ttft_ms": _numeric_summary(phase_values("ttft_ms")),
        "generation_ms": _numeric_summary(phase_values("generation_ms")),
        "completion_ms": _numeric_summary([sample.completion_ms for sample in samples]),
        "error_counts": dict(sorted(error_counts.items())),
        "mode_counts": {
            mode: sum(sample.mode == mode for sample in samples)
            for mode in sorted({sample.mode for sample in samples})
        },
        "truncated_count": sum(sample.finish_reason == "length" for sample in samples),
        "cleanup_ms": _numeric_summary(phase_values("cleanup_ms")),
    }
    # 保留旧产物使用的简短字段，避免已有脚本读取 Fake 基线时发生无意义的
    # 兼容性破坏；完整、规范化的统计仍以 success_latency_ms 为准。
    summary["latency_ms"] = {
        "p50_nearest_rank": summary["success_latency_ms"]["p50_nearest_rank"],
        "p95_nearest_rank": summary["success_latency_ms"]["p95_nearest_rank"],
        "mean": summary["success_latency_ms"]["mean"],
    }
    return summary


def _fake_response(_: object) -> str:
    """返回固定、可逐字符核验的 JSON；不模拟真实模型随机性。"""

    return json.dumps(
        {
            "answer_status": "answered",
            "message": "由证据原文回答",
            # Python 切片的 end 是排他边界；“确定性证据”恰好包含 5 个
            # Unicode 字符，因此这里必须使用 end=5，不能凭视觉估计长度。
            "excerpts": [{"evidence_id": "doc-1", "quote": "确定性证据", "start": 0, "end": 5}],
        },
        ensure_ascii=False,
    )


async def run_fake(iterations: int) -> dict[str, Any]:
    """运行确定性 Fake 基线，只测应用层调用和 identity 校验耗时。"""

    if iterations <= 0:
        raise ValueError("iterations 必须大于 0")

    service = ExcerptAnswerService(FakeChatProvider(_fake_response))
    samples: list[BenchmarkSample] = []
    for index in range(1, iterations + 1):
        started = time.perf_counter()
        try:
            response = await service.answer(
                question="证据是什么？",
                evidence=[{"evidence_id": "doc-1", "text": "确定性证据"}],
            )
            completion_ms = (time.perf_counter() - started) * 1000
            valid = response.answer_status == "answered" and response.citation_validation == "passed"
            samples.append(
                BenchmarkSample(
                    mode="json",
                    index=index,
                    outcome="success" if valid else "failure",
                    completion_ms=completion_ms,
                    queue_ms=0.0,
                    ttft_ms=None,
                    generation_ms=completion_ms,
                    error_kind=None if valid else response.answer_status,
                    answer_status=response.answer_status,
                    citation_validation=response.citation_validation,
                    finish_reason=(response.provider_response.finish_reason if response.provider_response else None),
                )
            )
        except ProviderError as exc:
            samples.append(
                BenchmarkSample(
                    mode="json",
                    index=index,
                    outcome="failure",
                    completion_ms=(time.perf_counter() - started) * 1000,
                    queue_ms=0.0,
                    ttft_ms=None,
                    generation_ms=None,
                    error_kind=exc.kind.value,
                )
            )

    result: dict[str, Any] = summarize_samples(samples)
    result.update(
        {
            "provider": "fake",
            "iterations": iterations,
            "samples": [sample.to_dict() for sample in samples],
            "not_applicable": ["retrieval", "embedding", "reranker", "production_capacity"],
        }
    )
    return result


async def _await_if_needed(value: Any) -> Any:
    """兼容同步返回异步迭代器和返回 coroutine 的 Provider 实现。"""

    if inspect.isawaitable(value):
        return await value
    return value


async def _close_iterator(iterator: Any) -> None:
    """尽力关闭上游流；清理错误不能掩盖 benchmark 的主结果。"""

    close = getattr(iterator, "aclose", None)
    if close is None:
        return
    try:
        await _await_if_needed(close())
    except asyncio.CancelledError:
        # 取消本身优先于清理异常；调用方会继续记录取消结果并释放 lease。
        raise
    except Exception:
        # benchmark 只记录稳定的 error_kind，不把上游异常正文写入产物。
        return


async def _release_lease(lease: SchedulerLease) -> None:
    """在任务已收到取消信号时仍尽量完成 lease 释放。"""

    release_task = asyncio.create_task(lease.release())
    try:
        await asyncio.shield(release_task)
    except asyncio.CancelledError:
        # `shield` 防止外层取消直接取消 release task；这里等待一次结果，
        # 让 scheduler 的 active_count 不因为 benchmark 取消而永久偏大。
        try:
            await release_task
        except BaseException:
            return
    except Exception:
        return


def _benchmark_request(
    service: ExcerptAnswerService,
    *,
    model: str,
    max_tokens: int,
) -> tuple[ChatRequest, tuple[EvidenceInput, ...]]:
    """通过应用服务的正式入口构造请求，避免 benchmark 复制 Prompt 逻辑。"""

    return service.prepare_request(
        question=_BENCHMARK_QUESTION,
        evidence=_BENCHMARK_EVIDENCE,
        model=model,
        temperature=0.0,
        max_tokens=max_tokens,
    )


def _response_status(response: Any) -> tuple[str | None, str | None]:
    """提取应用层终态；异常对象不直接序列化到产物。"""

    answer_status = getattr(response, "answer_status", None)
    citation_validation = getattr(response, "citation_validation", None)
    return (
        answer_status if isinstance(answer_status, str) else None,
        citation_validation if isinstance(citation_validation, str) else None,
    )


def _response_is_success(response: Any) -> bool:
    """判断一次调用是否完成了可接受的应用层终态。"""

    answer_status, citation_validation = _response_status(response)
    # `abstained` 是完整且明确的拒答终态；它与 `answered` 一样表示请求
    # 已完成。真正的语义正确性仍不在本 benchmark 的能力范围内。
    return answer_status == "abstained" or (
        answer_status == "answered" and citation_validation == "passed"
    )


def _provider_usage(response: ChatResponse | None) -> tuple[int | None, int | None]:
    """安全读取可选 usage；缺失时保留 None，不推算 token 数。"""

    if response is None or response.usage is None:
        return None, None
    return response.usage.input_tokens, response.usage.output_tokens


def _failure_kind_from_response(response: Any) -> str:
    """把应用层非成功终态映射为稳定的 benchmark 分类。"""

    answer_status, _ = _response_status(response)
    if answer_status == "incomplete":
        return "truncated"
    if answer_status == "invalid_citations":
        return "invalid_citations"
    if answer_status in {"failed", "cancelled"}:
        return answer_status
    return "application_failure"


async def _run_json_sample(
    *,
    index: int,
    service: ExcerptAnswerService,
    provider: LMStudioChatProvider,
    scheduler: LocalLLMScheduler,
    request: ChatRequest,
    evidence: Sequence[EvidenceInput],
    deadline: float,
) -> BenchmarkSample:
    """运行一条 JSON 请求并记录排队、生成和完整终态耗时。"""

    started = time.perf_counter()
    lease: SchedulerLease | None = None
    acquired_at: float | None = None
    provider_response: ChatResponse | None = None
    answer_status: str | None = None
    citation_validation: str | None = None
    outcome = "failure"
    error_kind: str | None = "unknown"
    cancellation_requested = False
    cancellation_observed: bool | None = None

    try:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            raise asyncio.TimeoutError
        lease = await scheduler.acquire(queue_timeout=remaining)
        acquired_at = time.perf_counter()
        remaining = deadline - acquired_at
        if remaining <= 0:
            raise asyncio.TimeoutError

        # HTTPX 的 read timeout 只限制空闲读取；这里再加一层绝对 deadline，
        # 使模型持续输出但总耗时过长时也会被终止。
        provider_response = await asyncio.wait_for(
            provider.complete(request),
            timeout=remaining,
        )
        application_response = service.interpret_response(provider_response, evidence)
        answer_status, citation_validation = _response_status(application_response)
        if _response_is_success(application_response):
            outcome = "success"
            error_kind = None
        else:
            error_kind = _failure_kind_from_response(application_response)
    except asyncio.TimeoutError:
        error_kind = "timeout"
    except asyncio.CancelledError:
        # 只有 benchmark 自己或组级 deadline 取消任务时才进入这里；不把它
        # 伪装成 Provider 正常失败，便于单独统计取消率。
        outcome = "cancelled"
        error_kind = "cancelled"
        cancellation_requested = True
        cancellation_observed = True
    except ProviderError as exc:
        error_kind = exc.kind.value
        if exc.kind is ProviderErrorKind.CANCELLED:
            outcome = "cancelled"
            cancellation_observed = True
    except Exception:
        # benchmark 的错误产物只需要可聚合的分类，不应回显模型响应或
        # httpx 异常正文，以免本地证据/密钥进入日志。
        error_kind = "unknown"
    finally:
        if lease is not None:
            await _release_lease(lease)

    completed_at = time.perf_counter()
    input_tokens, output_tokens = _provider_usage(provider_response)
    return BenchmarkSample(
        mode="json",
        index=index,
        outcome=outcome,
        completion_ms=max(0.0, (completed_at - started) * 1000),
        queue_ms=lease.queue_ms if lease is not None else None,
        ttft_ms=None,
        generation_ms=(
            max(0.0, (completed_at - acquired_at) * 1000) if acquired_at is not None else None
        ),
        error_kind=error_kind,
        finish_reason=provider_response.finish_reason if provider_response else None,
        answer_status=answer_status,
        citation_validation=citation_validation,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cancellation_requested=cancellation_requested,
        cancellation_observed=cancellation_observed,
    )


async def _stream_iterator(
    provider: LMStudioChatProvider,
    request: ChatRequest,
) -> tuple[AsyncIterator[Any], bool]:
    """优先使用结构化流；返回值同时标记是否需要终态事件。"""

    structured = getattr(provider, "stream_events", None)
    if callable(structured):
        iterator = await _await_if_needed(structured(request))
        return iterator, True
    iterator = await _await_if_needed(provider.stream(request))
    return iterator, False


async def _consume_stream(
    *,
    provider: LMStudioChatProvider,
    request: ChatRequest,
    started: float,
    first_visible: asyncio.Event | None = None,
    on_visible: Callable[[str], None] | None = None,
) -> _StreamObservation:
    """消费一次 Provider 流，并只保留可见正文和终态元数据。

    `VisibleStreamSanitizer` 会处理跨 chunk 的 reasoning/代码围栏；这里再
    明确要求结构化 Provider 发出 done 事件，防止普通 EOF 被误判为成功。
    原始 SSE JSON、reasoning 字段和异常正文都不会进入返回值。
    """

    iterator, structured = await _stream_iterator(provider, request)
    sanitizer = VisibleStreamSanitizer()
    pieces: list[str] = []
    finish_reason: str | None = None
    usage: ChatUsage | None = None
    ttft_ms: float | None = None
    chunk_count = 0
    saw_terminal_event = False

    try:
        async for item in iterator:
            chunk_count += 1
            if isinstance(item, ChatStreamEvent):
                text = item.text
                if item.finish_reason is not None:
                    finish_reason = item.finish_reason
                if item.usage is not None:
                    usage = item.usage
                if item.done:
                    saw_terminal_event = True
            elif isinstance(item, str):
                # 仅为旧式字符串 Provider 保留兼容路径；真实 LM Studio
                # adapter 使用上面的结构化事件，因此仍会验证 done。
                text = item
            else:
                raise ProviderError(ProviderErrorKind.PROTOCOL, "Provider 流事件格式非法")

            visible = sanitizer.push(text)
            if visible:
                if ttft_ms is None:
                    ttft_ms = max(0.0, (time.perf_counter() - started) * 1000)
                    if first_visible is not None:
                        first_visible.set()
                if on_visible is not None:
                    on_visible(visible)
                pieces.append(visible)

            if isinstance(item, ChatStreamEvent) and item.done:
                # 终态事件之后不再读取额外正文；finally 仍会关闭迭代器，
                # 以覆盖上游 generator 或 HTTP response 的清理语义。
                break

        try:
            tail = sanitizer.finish()
        except ProviderError as exc:
            # LM Studio 在达到 max_tokens 时可能先发送 finish_reason=length，
            # 再以未闭合的 <think> 或代码围栏结束。截断信号已经足够说明
            # 结果不完整，此时丢弃未完成的包装尾巴并交给上层统一标记
            # `incomplete`；只有没有明确截断信号时才保留 protocol 错误。
            if finish_reason != "length" or exc.kind is not ProviderErrorKind.PROTOCOL:
                raise
            tail = ""
        if tail:
            if ttft_ms is None:
                ttft_ms = max(0.0, (time.perf_counter() - started) * 1000)
                if first_visible is not None:
                    first_visible.set()
            if on_visible is not None:
                on_visible(tail)
            pieces.append(tail)

        if structured and not saw_terminal_event:
            raise ProviderError(ProviderErrorKind.PROTOCOL, "结构化流未收到终态事件")
        if not structured and finish_reason is None:
            # 旧式字符串接口没有 finish_reason 字段；它只作为测试兼容
            # 路径使用，完整消费后可记录一个明确的本地假设。
            finish_reason = "stop"

        return _StreamObservation(
            visible_text="".join(pieces),
            finish_reason=finish_reason,
            usage=usage,
            ttft_ms=ttft_ms,
            chunk_count=chunk_count,
            saw_terminal_event=saw_terminal_event,
        )
    finally:
        await _close_iterator(iterator)


async def _run_sse_sample(
    *,
    index: int,
    service: ExcerptAnswerService,
    provider: LMStudioChatProvider,
    scheduler: LocalLLMScheduler,
    request: ChatRequest,
    evidence: Sequence[EvidenceInput],
    deadline: float,
) -> BenchmarkSample:
    """运行一条 SSE 请求，TTFT 只从首个可见正文增量开始计算。"""

    started = time.perf_counter()
    lease: SchedulerLease | None = None
    acquired_at: float | None = None
    observation: _StreamObservation | None = None
    provider_response: ChatResponse | None = None
    answer_status: str | None = None
    citation_validation: str | None = None
    outcome = "failure"
    error_kind: str | None = "unknown"
    cancellation_requested = False
    cancellation_observed: bool | None = None

    try:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            raise asyncio.TimeoutError
        lease = await scheduler.acquire(queue_timeout=remaining)
        acquired_at = time.perf_counter()
        remaining = deadline - acquired_at
        if remaining <= 0:
            raise asyncio.TimeoutError

        observation = await asyncio.wait_for(
            _consume_stream(
                provider=provider,
                request=request,
                started=started,
            ),
            timeout=remaining,
        )
        provider_response = ChatResponse(
            content=observation.visible_text,
            model=request.model,
            finish_reason=observation.finish_reason,
            usage=observation.usage,
        )
        application_response = service.interpret_response(provider_response, evidence)
        answer_status, citation_validation = _response_status(application_response)
        if _response_is_success(application_response):
            outcome = "success"
            error_kind = None
        else:
            error_kind = _failure_kind_from_response(application_response)
    except asyncio.TimeoutError:
        error_kind = "timeout"
    except asyncio.CancelledError:
        outcome = "cancelled"
        error_kind = "cancelled"
        cancellation_requested = True
        cancellation_observed = True
    except ProviderError as exc:
        error_kind = exc.kind.value
        if exc.kind is ProviderErrorKind.CANCELLED:
            outcome = "cancelled"
            cancellation_observed = True
    except Exception:
        error_kind = "unknown"
    finally:
        if lease is not None:
            await _release_lease(lease)

    completed_at = time.perf_counter()
    input_tokens, output_tokens = _provider_usage(provider_response)
    return BenchmarkSample(
        mode="sse",
        index=index,
        outcome=outcome,
        completion_ms=max(0.0, (completed_at - started) * 1000),
        queue_ms=lease.queue_ms if lease is not None else None,
        ttft_ms=observation.ttft_ms if observation is not None else None,
        generation_ms=(
            max(0.0, (completed_at - acquired_at) * 1000) if acquired_at is not None else None
        ),
        error_kind=error_kind,
        finish_reason=(observation.finish_reason if observation is not None else None),
        answer_status=answer_status,
        citation_validation=citation_validation,
        visible_chars=(len(observation.visible_text) if observation is not None else 0),
        chunk_count=(observation.chunk_count if observation is not None else 0),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cancellation_requested=cancellation_requested,
        cancellation_observed=cancellation_observed,
    )


async def _run_cancel_sample(
    *,
    index: int,
    service: ExcerptAnswerService,
    provider: LMStudioChatProvider,
    scheduler: LocalLLMScheduler,
    request: ChatRequest,
    evidence: Sequence[EvidenceInput],
    deadline: float,
) -> BenchmarkSample:
    """执行一次主动取消实验，并单独测量取消后的清理耗时。

    取消实验不把“客户端 task 被取消”夸大成“GPU 已停止生成”。这里只记录
    应用 task、异步迭代器和 scheduler lease 是否回收；上游运行时的 GPU 停止
    语义仍必须在模型服务自身提供证据后才能下结论。
    """

    started = time.perf_counter()
    first_visible = asyncio.Event()
    visible_chars = 0
    chunk_count = 0
    ttft_ms: float | None = None
    lease: SchedulerLease | None = None
    acquired_at: float | None = None

    def record_visible(text: str) -> None:
        """记录取消前已经交给客户端的可见正文长度。"""

        nonlocal visible_chars, ttft_ms
        visible_chars += len(text)
        if ttft_ms is None:
            ttft_ms = max(0.0, (time.perf_counter() - started) * 1000)

    async def consume() -> _StreamObservation:
        """在独立 task 中持有 lease，确保取消时进入统一清理路径。"""

        nonlocal lease, acquired_at, chunk_count
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            raise asyncio.TimeoutError
        lease = await scheduler.acquire(queue_timeout=remaining)
        acquired_at = time.perf_counter()
        try:
            observation = await asyncio.wait_for(
                _consume_stream(
                    provider=provider,
                    request=request,
                    started=started,
                    first_visible=first_visible,
                    on_visible=record_visible,
                ),
                timeout=max(0.0, deadline - acquired_at),
            )
            chunk_count = observation.chunk_count
            return observation
        finally:
            await _release_lease(lease)

    task = asyncio.create_task(consume())
    cancellation_requested = False
    cancellation_observed: bool | None = None
    observation: _StreamObservation | None = None
    error_kind: str | None = None
    outcome = "failure"
    answer_status: str | None = None
    citation_validation: str | None = None
    finish_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None

    # 等待首个可见 token 只是为了让取消更接近“已经开始生成”的场景。
    # 这里必须显式持有独立的 waiter task：直接对 ``shield(event.wait())``
    # 使用 ``wait_for`` 时，超时只会取消外层 shield，内部 ``Event.wait``
    # 可能一直留在事件循环中，最终在 CLI 退出时产生 pending-task 警告。
    # waiter 与模型消费 task 分离，清理 waiter 不会误取消真正的模型请求。
    first_visible_waiter = asyncio.create_task(first_visible.wait())
    trigger_timeout = min(
        _DEFAULT_CANCEL_TRIGGER_SECONDS,
        max(0.01, deadline - time.perf_counter()),
    )
    try:
        await asyncio.wait_for(first_visible_waiter, timeout=trigger_timeout)
    except asyncio.TimeoutError:
        pass
    finally:
        # ``wait_for`` 超时通常已经取消 waiter，但这里仍显式检查并收拢
        # task，覆盖首 token 已到达、外层取消和事件循环关闭等所有路径。
        if not first_visible_waiter.done():
            first_visible_waiter.cancel()
        await asyncio.gather(first_visible_waiter, return_exceptions=True)

    if not task.done():
        cancellation_requested = True
        cancel_started = time.perf_counter()
        task.cancel()
        try:
            observation = await asyncio.wait_for(
                asyncio.shield(task),
                timeout=_DEFAULT_CANCEL_CLEANUP_TIMEOUT_SECONDS,
            )
            # 模型在取消请求到达前自然结束，不能伪称为取消成功；保留
            # 正常终态，供报告显示“取消请求未命中”。
            cancellation_observed = False
        except asyncio.CancelledError:
            cancellation_observed = True
            outcome = "cancelled"
            error_kind = "cancelled"
        except asyncio.TimeoutError:
            # 再发一次取消并收拢 task，避免 benchmark 自己留下后台任务。
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            cancellation_observed = False
            outcome = "failure"
            error_kind = "cleanup_timeout"
        except ProviderError:
            # 某些异步生成器会在 finally 中吞掉 CancelledError，再把清理阶段的
            # protocol 异常抛回调用方。此时本地 task 已在显式取消后终止，应记录
            # 为取消已观察到；这只描述应用清理结果，不宣称上游 GPU 已停止生成。
            cancellation_observed = True
            outcome = "cancelled"
            error_kind = "cancelled"
        except Exception:
            # 与上一个分支相同：取消请求后 task 已退出，即使上游异常类型不是
            # ProviderError，也不能把本地取消清理误报为普通失败。
            cancellation_observed = True
            outcome = "cancelled"
            error_kind = "cancelled"
        cleanup_ms = max(0.0, (time.perf_counter() - cancel_started) * 1000)
    else:
        cleanup_ms = None
        try:
            observation = task.result()
            cancellation_observed = False
        except asyncio.TimeoutError:
            error_kind = "timeout"
        except ProviderError as exc:
            error_kind = exc.kind.value
        except asyncio.CancelledError:
            outcome = "cancelled"
            error_kind = "cancelled"
            cancellation_observed = True
        except Exception:
            error_kind = "unknown"

    if observation is not None and outcome != "cancelled" and error_kind is None:
        finish_reason = observation.finish_reason
        input_tokens = observation.usage.input_tokens if observation.usage else None
        output_tokens = observation.usage.output_tokens if observation.usage else None
        try:
            provider_response = ChatResponse(
                content=observation.visible_text,
                model=request.model,
                finish_reason=observation.finish_reason,
                usage=observation.usage,
            )
            application_response = service.interpret_response(provider_response, evidence)
            answer_status, citation_validation = _response_status(application_response)
            if _response_is_success(application_response):
                outcome = "success"
            else:
                outcome = "failure"
                error_kind = _failure_kind_from_response(application_response)
        except ProviderError as exc:
            outcome = "failure"
            error_kind = exc.kind.value

    completed_at = time.perf_counter()
    return BenchmarkSample(
        mode="sse_cancel",
        index=index,
        outcome=outcome,
        completion_ms=max(0.0, (completed_at - started) * 1000),
        queue_ms=lease.queue_ms if lease is not None else None,
        ttft_ms=ttft_ms,
        generation_ms=(
            max(0.0, (completed_at - acquired_at) * 1000) if acquired_at is not None else None
        ),
        error_kind=error_kind,
        finish_reason=finish_reason,
        answer_status=answer_status,
        citation_validation=citation_validation,
        cleanup_ms=cleanup_ms,
        visible_chars=visible_chars,
        chunk_count=chunk_count,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cancellation_requested=cancellation_requested,
        cancellation_observed=cancellation_observed,
    )


def _unexpected_sample(mode: str, index: int, error_kind: str = "unknown") -> BenchmarkSample:
    """将 worker 意外抛出的异常转换为可聚合样本，避免整组统计丢失。"""

    return BenchmarkSample(
        mode=mode,
        index=index,
        outcome="failure",
        completion_ms=0.0,
        queue_ms=None,
        ttft_ms=None,
        generation_ms=None,
        error_kind=error_kind,
    )


async def _run_group(
    *,
    mode: str,
    iterations: int,
    concurrency: int,
    group_timeout_seconds: float,
    service: ExcerptAnswerService,
    provider: LMStudioChatProvider,
    request: ChatRequest,
    evidence: Sequence[EvidenceInput],
) -> dict[str, Any]:
    """运行一个固定并发组；组级超时会取消剩余 task 并保留其样本。"""

    scheduler = LocalLLMScheduler(
        provider,
        max_concurrency=concurrency,
        # 这里把队列上限设为本组样本数，避免 benchmark 因测试规模本身
        # 超过默认 8 而测到“队列已满”，除非调用方明确要做该实验。
        max_queue_size=max(0, iterations),
    )
    started = time.perf_counter()
    deadline = started + group_timeout_seconds
    worker = _run_json_sample if mode == "json" else _run_sse_sample
    tasks = [
        asyncio.create_task(
            worker(
                index=index,
                service=service,
                provider=provider,
                scheduler=scheduler,
                request=request,
                evidence=evidence,
                deadline=deadline,
            )
        )
        for index in range(1, iterations + 1)
    ]
    done, pending = await asyncio.wait(tasks, timeout=group_timeout_seconds)
    if pending:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    samples: list[BenchmarkSample] = []
    for index, task in enumerate(tasks, 1):
        if not task.done():
            samples.append(_unexpected_sample(mode, index, "group_timeout"))
            continue
        try:
            value = task.result()
        except asyncio.CancelledError:
            value = BenchmarkSample(
                mode=mode,
                index=index,
                outcome="cancelled",
                completion_ms=max(0.0, (time.perf_counter() - started) * 1000),
                queue_ms=None,
                ttft_ms=None,
                generation_ms=None,
                error_kind="cancelled",
                cancellation_requested=True,
                cancellation_observed=True,
            )
        except Exception:
            value = _unexpected_sample(mode, index)
        if isinstance(value, BenchmarkSample):
            samples.append(value)
        else:
            samples.append(_unexpected_sample(mode, index))

    samples.sort(key=lambda sample: sample.index)
    wall_time_ms = max(0.0, (time.perf_counter() - started) * 1000)
    summary = summarize_samples(samples)
    summary["wall_time_ms"] = wall_time_ms
    summary["completed_throughput_per_second"] = (
        summary["success_count"] / (wall_time_ms / 1000)
        if wall_time_ms > 0
        else None
    )
    summary["usage_observed_count"] = sum(
        sample.input_tokens is not None or sample.output_tokens is not None for sample in samples
    )
    summary["usage_missing_count"] = sum(
        sample.input_tokens is None and sample.output_tokens is None for sample in samples
    )
    return {
        "mode": mode,
        "concurrency": concurrency,
        "iterations": iterations,
        "group_timeout_seconds": group_timeout_seconds,
        "summary": summary,
        "samples": [sample.to_dict() for sample in samples],
    }


async def _discover_models(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    model: str,
    api_key: str | None,
) -> dict[str, Any]:
    """只读探测 LM Studio 模型列表，并返回不含原始响应的摘要。"""

    headers = {"content-type": "application/json"}
    if api_key:
        headers["authorization"] = "Bearer <configured>"
    try:
        response = await client.get(
            f"{_normalized_api_base(base_url)}/models",
            headers=headers,
            timeout=5.0,
        )
        if response.status_code >= 400:
            return {
                "ok": False,
                "error_kind": "unavailable",
                "status_code": response.status_code,
                "model_count": 0,
                "target_present": False,
            }
        payload = response.json()
        raw_models = payload.get("data", []) if isinstance(payload, dict) else []
        model_ids = {
            item.get("id")
            for item in raw_models
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        return {
            "ok": True,
            "error_kind": None,
            "status_code": response.status_code,
            "model_count": len(model_ids),
            "target_present": model in model_ids,
        }
    except httpx.TimeoutException:
        return {
            "ok": False,
            "error_kind": "timeout",
            "status_code": None,
            "model_count": 0,
            "target_present": False,
        }
    except (httpx.HTTPError, ValueError, TypeError):
        return {
            "ok": False,
            "error_kind": "protocol",
            "status_code": None,
            "model_count": 0,
            "target_present": False,
        }


def _make_local_client(max_connections: int) -> httpx.AsyncClient:
    """创建共享 HTTPX client；连接池大小与 benchmark 并发保持有限。"""

    timeout = httpx.Timeout(
        connect=5.0,
        read=60.0,
        write=10.0,
        pool=5.0,
    )
    limits = httpx.Limits(
        max_connections=max(4, max_connections * 2),
        max_keepalive_connections=max(4, max_connections),
    )
    return httpx.AsyncClient(timeout=timeout, limits=limits)


async def run_local(
    *,
    base_url: str | None = None,
    model: str | None = None,
    iterations: int = 10,
    max_tokens: int = 128,
    concurrency_levels: Sequence[int] = _DEFAULT_CONCURRENCY_LEVELS,
    warmup: int = 1,
    cancellation_runs: int = 2,
    group_timeout_seconds: float = _DEFAULT_GROUP_TIMEOUT_SECONDS,
    api_key: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """运行真实 LM Studio 的 JSON/SSE 有界 benchmark。

    默认配置严格对应 F2-G 设计：并发 1/2、每组 10 次、128 token 上限、
    每种协议一次 warmup、两次独立主动取消。函数允许注入 HTTPX client，
    便于用 MockTransport 做离线契约测试；正常 CLI 不注入 client，也不会
    自动调用在线 Provider。
    """

    resolved_base_url = base_url or os.getenv("LOCAL_LLM_BASE_URL", "http://127.0.0.1:1234")
    resolved_model = model or os.getenv("LOCAL_LLM_MODEL", "")
    resolved_api_key = api_key if api_key is not None else (os.getenv("LOCAL_LLM_API_KEY") or None)
    levels = tuple(sorted(set(concurrency_levels)))
    if not resolved_model:
        raise ValueError("run_local 必须提供 model 或设置 LOCAL_LLM_MODEL")
    if iterations <= 0:
        raise ValueError("iterations 必须大于 0")
    if max_tokens <= 0:
        raise ValueError("max_tokens 必须大于 0")
    if not levels or any(level <= 0 for level in levels):
        raise ValueError("concurrency_levels 必须包含正整数")
    if warmup < 0 or cancellation_runs < 0:
        raise ValueError("warmup 和 cancellation_runs 不能小于 0")
    if group_timeout_seconds <= 0:
        raise ValueError("group_timeout_seconds 必须大于 0")

    started_at = _utc_timestamp()
    owned_client = client is None
    http_client = client or _make_local_client(max(levels))
    provider = LMStudioChatProvider(
        http_client,
        base_url=resolved_base_url,
        model=resolved_model,
        api_key=resolved_api_key,
    )
    service = ExcerptAnswerService(provider)
    request, evidence = _benchmark_request(service, model=resolved_model, max_tokens=max_tokens)

    result: dict[str, Any] = {
        "provider": "local_lmstudio",
        "status": "starting",
        "model": resolved_model,
        "base_url": _normalized_api_base(resolved_base_url),
        "model_discovery": None,
        "warmup": [],
        "groups": [],
        "cancellation": {"summary": summarize_samples([]), "samples": []},
        "not_applicable": ["retrieval", "embedding", "reranker", "production_capacity"],
        "unmeasured": ["gpu_memory", "power", "cost", "upstream_gpu_stop"],
        "warnings": [
            "结果仅代表当前机器、当前 LM Studio 进程、当前模型和当前配置",
            "取消清理完成不等于已证明上游 GPU 停止生成",
            "usage 缺失时不推算 token 吞吐",
        ],
    }

    try:
        discovery = await _discover_models(
            http_client,
            base_url=resolved_base_url,
            model=resolved_model,
            api_key=resolved_api_key,
        )
        result["model_discovery"] = discovery
        if not discovery["ok"]:
            result["status"] = "environment_unavailable"
            result["not_run_reason"] = "LM Studio /v1/models 探测失败"
            return result
        if not discovery["target_present"]:
            result["status"] = "environment_unavailable"
            result["not_run_reason"] = "目标模型不在 /v1/models 列表中"
            return result

        # warmup 每种协议各运行一次，但始终单独放在 `warmup` 字段，不混进
        # 正式并发组的成功延迟分布。这样 JSON/SSE 的连接建立成本都被显式看见。
        if warmup:
            warmup_scheduler = LocalLLMScheduler(
                provider,
                max_concurrency=1,
                max_queue_size=1,
            )
            for index in range(1, warmup + 1):
                warmup_deadline = time.perf_counter() + group_timeout_seconds
                result["warmup"].append(
                    {
                        "mode": "json",
                        "sample": (
                            await _run_json_sample(
                                index=index,
                                service=service,
                                provider=provider,
                                scheduler=warmup_scheduler,
                                request=request,
                                evidence=evidence,
                                deadline=warmup_deadline,
                            )
                        ).to_dict(),
                    }
                )
                warmup_deadline = time.perf_counter() + group_timeout_seconds
                result["warmup"].append(
                    {
                        "mode": "sse",
                        "sample": (
                            await _run_sse_sample(
                                index=index,
                                service=service,
                                provider=provider,
                                scheduler=warmup_scheduler,
                                request=request,
                                evidence=evidence,
                                deadline=warmup_deadline,
                            )
                        ).to_dict(),
                    }
                )

        for mode in ("json", "sse"):
            for concurrency in levels:
                result["groups"].append(
                    await _run_group(
                        mode=mode,
                        iterations=iterations,
                        concurrency=concurrency,
                        group_timeout_seconds=group_timeout_seconds,
                        service=service,
                        provider=provider,
                        request=request,
                        evidence=evidence,
                    )
                )

        cancel_scheduler = LocalLLMScheduler(
            provider,
            max_concurrency=1,
            max_queue_size=1,
        )
        cancellation_samples: list[BenchmarkSample] = []
        for index in range(1, cancellation_runs + 1):
            cancellation_samples.append(
                await _run_cancel_sample(
                    index=index,
                    service=service,
                    provider=provider,
                    scheduler=cancel_scheduler,
                    request=request,
                    evidence=evidence,
                    deadline=time.perf_counter() + group_timeout_seconds,
                )
            )
        result["cancellation"] = {
            "summary": summarize_samples(cancellation_samples),
            "samples": [sample.to_dict() for sample in cancellation_samples],
        }

        normal_samples = [
            BenchmarkSample(**sample)
            for group in result["groups"]
            for sample in group["samples"]
        ]
        result["normal_summary"] = summarize_samples(normal_samples)
        result["status"] = "completed"
        return result
    finally:
        result["finished_at"] = _utc_timestamp()
        result["metadata"] = _build_metadata(
            base_url=resolved_base_url,
            model=resolved_model,
            max_tokens=max_tokens,
            concurrency_levels=levels,
            iterations=iterations,
            warmup=warmup,
            cancellation_runs=cancellation_runs,
            group_timeout_seconds=group_timeout_seconds,
            started_at=started_at,
            finished_at=result["finished_at"],
        )
        if owned_client:
            await http_client.aclose()


def main() -> None:
    """运行 Fake 或真实本地 benchmark，并把结构化结果写到 stdout/文件。"""

    parser = argparse.ArgumentParser(description="F2-G 原文摘录问答有界 benchmark")
    parser.add_argument(
        "--provider",
        choices=["fake", "local"],
        default="fake",
        help="fake 为确定性应用基线；local 才会调用本机 LM Studio",
    )
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument(
        "--concurrency",
        type=int,
        nargs="+",
        default=list(_DEFAULT_CONCURRENCY_LEVELS),
        help="local 模式的并发组，例如 --concurrency 1 2",
    )
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--cancel-runs", type=int, default=2)
    parser.add_argument("--group-timeout-seconds", type=float, default=_DEFAULT_GROUP_TIMEOUT_SECONDS)
    parser.add_argument("--base-url", help="LM Studio 根地址，默认读取 LOCAL_LLM_BASE_URL")
    parser.add_argument("--model", help="LM Studio 模型名，默认读取 LOCAL_LLM_MODEL")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.iterations <= 0:
        raise SystemExit("--iterations 必须大于 0")

    if args.provider == "fake":
        result = asyncio.run(run_fake(args.iterations))
    else:
        try:
            result = asyncio.run(
                run_local(
                    base_url=args.base_url,
                    model=args.model,
                    iterations=args.iterations,
                    max_tokens=args.max_tokens,
                    concurrency_levels=args.concurrency,
                    warmup=args.warmup,
                    cancellation_runs=args.cancel_runs,
                    group_timeout_seconds=args.group_timeout_seconds,
                )
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc

    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)


if __name__ == "__main__":
    main()
