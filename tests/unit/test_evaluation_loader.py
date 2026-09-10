from pathlib import Path

from enterprise_rag.evaluation.loader import load_jsonl


def test_load_draft_dataset():
    path = Path("docs/evaluation/dataset-draft.jsonl")
    cases = load_jsonl(path)
    assert len(cases) == 40
    assert len({case.id for case in cases}) == 40
    assert {case.answerability for case in cases} == {
        "answerable",
        "unanswerable",
        "conflict",
        "forbidden",
    }
