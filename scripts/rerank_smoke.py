#!/usr/bin/env python3
"""验证 BGE reranker v2 M3 可加载并对候选文档排序。"""

from enterprise_rag.reranking.bge import BGEReranker

query = "什么是企业知识库？"
documents = [
    "企业知识库用于集中管理企业内部文档和知识。",
    "今天天气很好，适合户外活动。",
    "Reranker 只能重排已经召回的候选文档。",
]

reranker = BGEReranker()
for result in reranker.rerank(query, documents):
    print(f"{result.score:.6f}\t{result.index}\t{result.text}")
