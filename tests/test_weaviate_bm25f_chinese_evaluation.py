"""BM25F 中文 tokenizer 与查询预处理对照实验的确定性契约测试。"""

from __future__ import annotations

from pathlib import Path

from scripts.weaviate_bm25f_chinese_evaluation import (
    _experiment_profiles,
    _query_text,
    _sparse_objects,
    preprocess_bm25_text,
)
from scripts.weaviate_bm25f_rrf_evaluation import load_quality_dataset


def test_experiment_profiles_change_one_primary_variable_at_a_time() -> None:
    """两个候选都必须只相对 baseline 改变 tokenizer 或预处理之一。"""

    profiles = _experiment_profiles()

    assert set(profiles) == {
        "word_identity",
        "trigram_identity",
        "word_cjk_bigram",
    }
    assert profiles["word_identity"].tokenization == "word"
    assert profiles["word_identity"].preprocessing == "identity"
    assert profiles["trigram_identity"].tokenization == "trigram"
    assert profiles["trigram_identity"].preprocessing == "identity"
    assert profiles["word_cjk_bigram"].tokenization == "word"
    assert profiles["word_cjk_bigram"].preprocessing == "cjk_bigram"
    assert len({profile.sparse_profile.key for profile in profiles.values()}) == 3


def test_cjk_bigram_preprocessing_is_stable_and_preserves_technical_tokens() -> None:
    """中文 bigram 预处理必须稳定，且不能拆坏 API、错误码和路径 token。"""

    text = "员工权限撤销后调用 CreateUser，返回 ERR_401 和 /v1/users。"

    first = preprocess_bm25_text(text, strategy="cjk_bigram")
    second = preprocess_bm25_text(text, strategy="cjk_bigram")

    assert first == second
    assert "员工" in first
    assert "权限" in first
    assert "撤销" in first
    assert "CreateUser" in first
    assert "ERR_401" in first
    assert "/v1/users" in first


def test_preprocessing_handles_empty_input_and_identity_normalization() -> None:
    """空输入必须稳定返回空串，identity 只规范空白而不改变 token。"""

    assert preprocess_bm25_text("   \n\t", strategy="cjk_bigram") == ""
    assert (
        preprocess_bm25_text(
            "  CreateUser   ERR_401   /v1/users  ",
            strategy="identity",
        )
        == "CreateUser ERR_401 /v1/users"
    )


def test_index_and_query_use_the_same_profile_preprocessing() -> None:
    """索引字段与查询文本必须由同一 profile 的预处理策略生成。"""

    dataset = load_quality_dataset(
        Path("docs/evaluation/bm25f-rrf-smoke-dataset.json")
    )
    profile = _experiment_profiles()["word_cjk_bigram"]

    objects = _sparse_objects(
        dataset,
        generation_id="generation-1",
        profile=profile,
    )
    query_text = _query_text(dataset.queries[0].text, profile=profile)

    assert objects[0].properties["body"] == preprocess_bm25_text(
        dataset.documents[0].body,
        strategy="cjk_bigram",
    )
    assert query_text == preprocess_bm25_text(
        dataset.queries[0].text,
        strategy="cjk_bigram",
    )


def test_experiment_artifact_records_reproducible_command_and_boundaries() -> None:
    """已保存产物必须包含可执行复现命令和不外推生产效果的边界。"""

    artifact_dir = Path(
        "artifacts/experiments/2026-09-17_bm25f-chinese-tokenization"
    )
    readme = (artifact_dir / "README.md").read_text(encoding="utf-8")
    conclusion = (artifact_dir / "conclusion.md").read_text(encoding="utf-8")

    assert "scripts/weaviate_bm25f_chinese_evaluation.py" in readme
    assert "不能代表真实企业语料" in readme
    assert "## 已确认" in conclusion
    assert "## 已验证" in conclusion
    assert "## 待实验" in conclusion
    assert "## 非目标" in conclusion
