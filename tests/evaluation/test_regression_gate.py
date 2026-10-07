"""P1 安全与可靠性回归门禁测试。"""

import json
from pathlib import Path

import pytest

from enterprise_rag.evaluation.regression_gate import (
    load_regression_manifest,
    run_regression_gate,
)


def _manifest() -> dict:
    """构造只包含脱敏测试节点的最小门禁清单。"""

    return {
        "schema_version": 1,
        "required_scenarios": [
            {
                "scenario_id": "tenant-isolation",
                "category": "security",
                "tests": ["tests/unit/test_authorization.py::test_role_gate"],
            },
            {
                "scenario_id": "retry-replay",
                "category": "reliability",
                "tests": ["tests/infrastructure/test_task_ledger.py::test_retry"],
            },
        ],
    }


def _junit(*testcases: str) -> str:
    """构造最小 JUnit XML；参数格式为 ``node_id|status``。"""

    cases = []
    for item in testcases:
        node_id, status = item.split("|", 1)
        file_name, test_name = node_id.split("::", 1)
        detail = {
            "passed": "",
            "failed": '<failure message="assertion failed" />',
            "error": '<error message="runtime error" />',
            "skipped": '<skipped message="not available" />',
        }[status]
        cases.append(
            f'<testcase classname="{file_name}" name="{test_name}">{detail}</testcase>'
        )
    return f'<testsuite name="pytest" tests="{len(cases)}">{"".join(cases)}</testsuite>'


def test_regression_gate_requires_all_required_scenarios_to_pass(tmp_path: Path) -> None:
    """所有场景通过时报告通过，并按类别汇总数量。"""

    manifest_path = tmp_path / "manifest.json"
    junit_path = tmp_path / "results.xml"
    manifest_path.write_text(json.dumps(_manifest()), encoding="utf-8")
    junit_path.write_text(
        _junit(
            "tests/unit/test_authorization.py::test_role_gate|passed",
            "tests/infrastructure/test_task_ledger.py::test_retry|passed",
        ),
        encoding="utf-8",
    )

    report = run_regression_gate(junit_path, manifest_path)

    assert report["passed"] is True
    assert report["scenario_count"] == 2
    assert report["categories"] == {
        "security": {"passed": 1, "failed": 0},
        "reliability": {"passed": 1, "failed": 0},
    }
    assert report["failures"] == []


def test_regression_gate_reports_missing_and_failed_tests(tmp_path: Path) -> None:
    """缺失测试或失败测试必须使门禁失败并保留原因。"""

    manifest_path = tmp_path / "manifest.json"
    junit_path = tmp_path / "results.xml"
    manifest_path.write_text(json.dumps(_manifest()), encoding="utf-8")
    junit_path.write_text(
        _junit("tests/unit/test_authorization.py::test_role_gate|failed"),
        encoding="utf-8",
    )

    report = run_regression_gate(junit_path, manifest_path)

    assert report["passed"] is False
    assert report["failures"] == [
        {
            "scenario_id": "tenant-isolation",
            "category": "security",
            "test": "tests/unit/test_authorization.py::test_role_gate",
            "status": "failed",
        },
        {
            "scenario_id": "retry-replay",
            "category": "reliability",
            "test": "tests/infrastructure/test_task_ledger.py::test_retry",
            "status": "missing",
        },
    ]


def test_regression_gate_rejects_duplicate_and_unknown_manifest_fields(tmp_path: Path) -> None:
    """清单重复场景、重复测试或未知字段必须在配置层失败。"""

    manifest = _manifest()
    manifest["required_scenarios"][0]["tests"].append(
        "tests/unit/test_authorization.py::test_role_gate"
    )
    manifest["extra"] = True
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    junit_path = tmp_path / "results.xml"
    junit_path.write_text(_junit("tests/unit/test_authorization.py::test_role_gate|passed"), encoding="utf-8")

    with pytest.raises(ValueError, match="未知字段"):
        load_regression_manifest(manifest_path)
