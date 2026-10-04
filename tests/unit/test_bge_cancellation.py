"""不加载权重验证BGE批次取消和CPU线程设置，真实计算另由运行验收证明。"""

import sys
from concurrent.futures import CancelledError
from contextlib import nullcontext
from threading import Event
from types import SimpleNamespace

import pytest

from enterprise_rag.reranking.bge import BGEReranker


def test_cpu_thread_budget_is_set_before_model_load(monkeypatch) -> None:
    """CPU模型必须在加载前收紧进程内PyTorch线程，避免占满全部核。"""
    calls = []
    model = SimpleNamespace(to=lambda device: calls.append(("to", device)), eval=lambda: None)
    loader = SimpleNamespace(from_pretrained=lambda _: model)
    torch = SimpleNamespace(
        set_num_threads=lambda value: calls.append(("threads", value)),
        backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: False)),
    )
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(
        AutoModelForSequenceClassification=loader, AutoTokenizer=loader,
    ))
    BGEReranker(device="cpu", cpu_threads=4)
    assert calls == [("threads", 4), ("to", "cpu")]


def test_cancelled_batch_does_not_run_remaining_batches() -> None:
    """取消无法中断当前native kernel，但当前批次结束后不得执行余下候选。"""
    cancel = Event()
    calls = []
    reranker = object.__new__(BGEReranker)
    reranker.device = "cpu"
    reranker._torch = SimpleNamespace(inference_mode=nullcontext)
    reranker.tokenizer = lambda *_args, **_kwargs: {}

    def model(**_kwargs):
        """模拟当前batch完成时收到取消，只返回当前批次合法分数。"""
        calls.append(1)
        cancel.set()
        logits = SimpleNamespace(
            view=lambda _: SimpleNamespace(
                float=lambda: SimpleNamespace(cpu=lambda: SimpleNamespace(tolist=lambda: [0.5])),
            ),
        )
        return SimpleNamespace(logits=logits)

    reranker.model = model
    with pytest.raises(CancelledError):
        reranker.rerank_cancellable("问题", ["一", "二", "三"], None, cancel, batch_size=1)
    assert len(calls) == 1
