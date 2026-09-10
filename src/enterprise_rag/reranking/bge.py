"""BGE reranker v2 M3 的本地 adapter。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence


@dataclass(frozen=True)
class RerankResult:
    """单个候选文档的 rerank 结果。"""

    index: int
    text: str
    score: float


class BGEReranker:
    """使用 Hugging Face 原生 sequence-classification 模型进行 rerank。"""

    def __init__(self, model_name: str = "BAAI/bge-reranker-v2-m3", device: Optional[str] = None):
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        self._torch = torch
        self.tokenizer = AutoTokenizer.from_pretrained(model_name)
        self.model = AutoModelForSequenceClassification.from_pretrained(model_name)
        self.device = device or ("mps" if torch.backends.mps.is_available() else "cpu")
        self.model.to(self.device)
        self.model.eval()

    def score(self, query: str, documents: Sequence[str], batch_size: int = 8) -> List[float]:
        """返回与 documents 顺序一致的相关性分数。"""
        scores: List[float] = []
        with self._torch.inference_mode():
            for start in range(0, len(documents), batch_size):
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
        return scores

    def rerank(
        self, query: str, documents: Sequence[str], top_k: Optional[int] = None, batch_size: int = 8
    ) -> List[RerankResult]:
        scores = self.score(query, documents, batch_size=batch_size)
        results = [RerankResult(i, text, score) for i, (text, score) in enumerate(zip(documents, scores))]
        results.sort(key=lambda item: item.score, reverse=True)
        return results[:top_k] if top_k is not None else results
