"""BM25F Spike 的确定性字段投影。

本模块只负责把现有 ``ParsedManifest`` 投影成可交给稀疏索引 transport 的对象。
它不访问数据库、不创建 Weaviate collection，也不改变默认 Dense 读取路径。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping


_API_SYMBOL_PATTERN = re.compile(
    r"(?<![\w])(?:/[A-Za-z0-9._~!$&'()*+,;=:@%/-]+|"
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+|"
    r"[A-Za-z]+(?:[_-][A-Za-z0-9]+)+|"
    r"[A-Za-z]*\d[A-Za-z0-9_-]*|"
    r"[A-Z][a-z]+[A-Z][A-Za-z0-9]*)(?![\w])"
)


@dataclass(frozen=True, slots=True)
class SparseProfile:
    """描述一次 BM25F Spike 使用的分词、预处理、schema 和字段权重版本。"""

    tokenizer_version: str
    preprocessing_version: str
    schema_version: str
    field_weights: Mapping[str, float]

    def __post_init__(self) -> None:
        """校验 profile，避免同一 profile key 对应不同字段策略。"""

        required_fields = {"title", "section_title", "api_symbols", "body"}
        if any(
            not value.strip()
            for value in (
                self.tokenizer_version,
                self.preprocessing_version,
                self.schema_version,
            )
        ):
            raise ValueError("sparse profile 版本不能为空")
        if set(self.field_weights) != required_fields:
            raise ValueError("sparse profile 字段权重必须覆盖固定 BM25F 字段")
        if any(weight <= 0 for weight in self.field_weights.values()):
            raise ValueError("sparse profile 字段权重必须大于 0")

    @property
    def key(self) -> str:
        """按固定顺序生成可持久化、可比较的 profile key。"""

        weights = ",".join(
            f"{name}={self.field_weights[name]:g}"
            for name in ("title", "section_title", "api_symbols", "body")
        )
        return ":".join(
            (
                self.tokenizer_version,
                self.preprocessing_version,
                self.schema_version,
                weights,
            )
        )


@dataclass(frozen=True, slots=True)
class SparseObject:
    """表示一个待交给 BM25F transport 的可幂等 sparse object。"""

    object_id: str
    properties: dict[str, Any]


class SparseFieldProjector:
    """把 parsed manifest 的 child chunk 投影为固定 BM25F 字段。"""

    def __init__(self, profile: SparseProfile) -> None:
        """保存本次 Spike 固定使用的 profile，不接受请求方临时覆盖。"""

        self.profile = profile

    @staticmethod
    def object_id(generation_id: str, chunk_id: str, profile_key: str) -> str:
        """为 generation、chunk 和 profile 生成稳定 object identity。"""

        value = f"{generation_id}:{chunk_id}:{profile_key}".encode("utf-8")
        return hashlib.sha256(value).hexdigest()

    @staticmethod
    def _manifest_hash(payload: Mapping[str, Any]) -> str:
        """用固定 JSON 编码计算 manifest hash，避免字典顺序影响结果。"""

        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _api_symbols(text: str) -> str:
        """提取含技术标识特征的 token，并按出现顺序去重。"""

        symbols: list[str] = []
        seen: set[str] = set()
        for match in _API_SYMBOL_PATTERN.finditer(text):
            symbol = match.group(0)
            if symbol not in seen:
                seen.add(symbol)
                symbols.append(symbol)
        return " ".join(symbols)

    @staticmethod
    def _heading_fields(heading_path: Any) -> tuple[str, str, list[str]]:
        """把标题路径规范化为 BM25F 的 title、section_title 和原始数组。"""

        if heading_path is None:
            values: list[str] = []
        elif isinstance(heading_path, (list, tuple)) and all(
            isinstance(value, str) for value in heading_path
        ):
            values = list(heading_path)
        else:
            raise ValueError("child heading_path 必须是字符串列表")
        title = values[0] if values else ""
        section_title = " > ".join(values)
        return title, section_title, values

    def project_manifest(
        self,
        payload: Mapping[str, Any],
        *,
        generation_id: str,
        tenant_id: str,
        document_id: str,
        version_id: str,
    ) -> list[SparseObject]:
        """校验 manifest scope，并为每个 child 生成确定性的 sparse object。"""

        expected_scope = (tenant_id, document_id, version_id)
        actual_scope = (
            payload.get("tenant_id"),
            payload.get("document_id"),
            payload.get("version_id"),
        )
        if actual_scope != expected_scope:
            raise ValueError("manifest scope 与 sparse index 请求不一致")
        if not generation_id.strip():
            raise ValueError("generation_id 不能为空")

        chunks = payload.get("chunks")
        if not isinstance(chunks, Mapping):
            raise ValueError("manifest chunks 必须是对象")
        children = chunks.get("children")
        if not isinstance(children, list):
            raise ValueError("manifest children 必须是列表")

        manifest_hash = self._manifest_hash(payload)
        projected: list[SparseObject] = []
        for child in sorted(children, key=lambda item: str(item.get("chunk_id", ""))):
            if not isinstance(child, Mapping):
                raise ValueError("manifest child 必须是对象")
            chunk_id = child.get("chunk_id")
            text = child.get("text")
            if not isinstance(chunk_id, str) or not chunk_id.strip():
                raise ValueError("child chunk_id 不能为空")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("child text 不能为空")
            if child.get("version_id") != version_id:
                raise ValueError("child version_id 与 manifest scope 不一致")

            title, section_title, heading_path = self._heading_fields(
                child.get("heading_path")
            )
            source_locators = child.get("source_locators", [])
            if not isinstance(source_locators, list):
                raise ValueError("child source_locators 必须是列表")

            # 字段和 lineage 均从当前 child 生成，不从调用方额外接收可检索正文。
            properties = {
                "tenant_id": tenant_id,
                "document_id": document_id,
                "version_id": version_id,
                "generation_id": generation_id,
                "sparse_profile": self.profile.key,
                "manifest_hash": manifest_hash,
                "chunk_id": chunk_id,
                "parent_id": child.get("parent_id"),
                "title": title,
                "section_title": section_title,
                "api_symbols": self._api_symbols(text),
                "body": text,
                "heading_path": heading_path,
                "source_locators": source_locators,
            }
            projected.append(
                SparseObject(
                    object_id=self.object_id(generation_id, chunk_id, self.profile.key),
                    properties=properties,
                )
            )
        return projected


__all__ = ["SparseFieldProjector", "SparseObject", "SparseProfile"]
