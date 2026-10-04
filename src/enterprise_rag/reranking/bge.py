"""BGE reranker v2 M3 的本地 adapter。"""

from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import CancelledError
from dataclasses import dataclass
from threading import Event


@dataclass(frozen=True)
class RerankResult:
    """单个候选文档的 rerank 结果。"""

    index: int
    text: str
    score: float


class BGEReranker:
    """使用 Hugging Face 原生 sequence-classification 模型进行 rerank。"""

    def __init__(
        self, model_name: str = "BAAI/bge-reranker-v2-m3",
        device: str | None = None, cpu_threads: int = 4,
    ):
        """加载模型；CPU模式在加载权重前限制进程内PyTorch计算线程。"""
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        self._torch = torch
        self.device = device or ("mps" if torch.backends.mps.is_available() else "cpu")
        if self.device == "cpu":
            torch.set_num_threads(cpu_threads)
        self.tokenizer = AutoTokenizer.from_pretrained(model_name)
        self.model = AutoModelForSequenceClassification.from_pretrained(model_name)
        self.model.to(self.device)
        self.model.eval()

    def score(
        self, query: str, documents: Sequence[str], batch_size: int = 8,
        cancel_event: Event | None = None,
    ) -> list[float]:
        """逐批计算分数；取消不能中断当前native kernel，但会停止后续批次。"""
        scores: list[float] = []
        with self._torch.inference_mode():
            for start in range(0, len(documents), batch_size):
                # 先检查取消，避免已离开的请求继续执行后续候选计算。
                if cancel_event is not None and cancel_event.is_set():
                    raise CancelledError()
                batch = list(documents[start : start + batch_size])
                encoded = self.tokenizer(
                    [query] * len(batch),
                    batch,
                    padding=True,
                    truncation=True,
                    return_tensors="pt",
                )
                encoded = {key: value.to(self.device) for key, value in encoded.items()}
                logits = self.model(**encoded, return_dict=True).logits
                scores.extend(logits.view(-1).float().cpu().tolist())
                if cancel_event is not None and cancel_event.is_set():
                    raise CancelledError()
        return scores

    def rerank(
        self, query: str, documents: Sequence[str], top_k: int | None = None, batch_size: int = 8
    ) -> list[RerankResult]:
        """保留同步调用接口，复用可取消路径的排序逻辑。"""
        return self.rerank_cancellable(query, documents, top_k, Event(), batch_size=batch_size)

    def rerank_cancellable(
        self, query: str, documents: Sequence[str], top_k: int | None,
        cancel_event: Event, batch_size: int = 8,
    ) -> list[RerankResult]:
        """在线程内接收请求取消信号，完成当前批次后停止计算。"""
        scores = self.score(query, documents, batch_size=batch_size, cancel_event=cancel_event)
        results = [RerankResult(i, text, score) for i, (text, score) in enumerate(zip(documents, scores))]
        results.sort(key=lambda item: item.score, reverse=True)
        return results[:top_k] if top_k is not None else results
