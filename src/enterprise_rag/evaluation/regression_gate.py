"""从 pytest JUnit 结果生成 P1 安全与可靠性回归门禁报告。"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree

_MANIFEST_FIELDS = {"schema_version", "required_scenarios"}
_SCENARIO_FIELDS = {"scenario_id", "category", "tests"}
_CATEGORIES = {"security", "reliability"}


def _require_string(value: Any, field_name: str) -> str:
    """校验非空字符串，避免清单中的节点 ID 被隐式转换。"""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 必须是非空字符串")
    return value


def _normalize_node_id(file_name: str, test_name: str) -> str:
    """把 JUnit 的文件/测试名属性规范化为稳定 pytest node ID。"""

    normalized_file = PurePosixPath(file_name).as_posix()
    normalized_file = normalized_file.removeprefix("./")
    if not normalized_file.endswith(".py"):
        normalized_file = f"{normalized_file}.py"
    return f"{normalized_file}::{test_name}"


def load_regression_manifest(path: str | Path) -> dict[str, Any]:
    """加载并严格校验 P1 回归场景清单。"""

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"回归清单不是有效 JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise TypeError("回归清单根节点必须是 JSON object")
    unknown = set(payload) - _MANIFEST_FIELDS
    if unknown:
        raise ValueError(f"回归清单存在未知字段: {sorted(unknown)}")
    if payload.get("schema_version") != 1:
        raise ValueError("回归清单 schema_version 必须为 1")

    raw_scenarios = payload.get("required_scenarios")
    if not isinstance(raw_scenarios, list) or not raw_scenarios:
        raise ValueError("required_scenarios 必须是非空列表")

    scenarios: list[dict[str, Any]] = []
    seen_scenarios: set[str] = set()
    seen_tests: set[str] = set()
    for index, raw_scenario in enumerate(raw_scenarios, start=1):
        if not isinstance(raw_scenario, dict):
            raise TypeError(f"第 {index} 个场景必须是 JSON object")
        unknown = set(raw_scenario) - _SCENARIO_FIELDS
        if unknown:
            raise ValueError(f"第 {index} 个场景存在未知字段: {sorted(unknown)}")
        scenario_id = _require_string(raw_scenario.get("scenario_id"), "scenario_id")
        if scenario_id in seen_scenarios:
            raise ValueError(f"重复 scenario_id: {scenario_id}")
        seen_scenarios.add(scenario_id)

        category = _require_string(raw_scenario.get("category"), "category")
        if category not in _CATEGORIES:
            raise ValueError(f"场景 {scenario_id} category 无效: {category}")

        raw_tests = raw_scenario.get("tests")
        if not isinstance(raw_tests, list) or not raw_tests:
            raise ValueError(f"场景 {scenario_id} tests 必须是非空列表")
        tests: list[str] = []
        for test in raw_tests:
            test_id = _require_string(test, f"场景 {scenario_id} 的 test")
            if "::" not in test_id:
                raise ValueError(f"场景 {scenario_id} 的 test 不是 pytest node ID: {test_id}")
            if test_id in seen_tests:
                raise ValueError(f"重复测试节点: {test_id}")
            seen_tests.add(test_id)
            tests.append(test_id)
        scenarios.append(
            {"scenario_id": scenario_id, "category": category, "tests": tuple(tests)}
        )

    return {"schema_version": 1, "required_scenarios": tuple(scenarios)}


def _load_junit_results(path: str | Path) -> dict[str, str]:
    """读取 JUnit XML，并将每个 pytest 节点映射到脱敏状态。"""

    try:
        root = ElementTree.parse(path).getroot()
    except ElementTree.ParseError as exc:
        raise ValueError(f"JUnit XML 无法解析: {exc}") from exc

    results: dict[str, str] = {}
    for testcase in root.findall(".//testcase"):
        file_name = testcase.attrib.get("file")
        if not file_name:
            classname = testcase.attrib.get("classname", "")
            file_name = (
                classname
                if "/" in classname or classname.endswith(".py")
                else classname.replace(".", "/")
            )
        test_name = testcase.attrib.get("name")
        if not file_name or not test_name:
            raise ValueError("JUnit testcase 缺少 file/classname 或 name")
        node_id = _normalize_node_id(file_name, test_name)
        if node_id in results:
            raise ValueError(f"JUnit XML 存在重复测试节点: {node_id}")
        if testcase.find("failure") is not None:
            status = "failed"
        elif testcase.find("error") is not None:
            status = "error"
        elif testcase.find("skipped") is not None:
            status = "skipped"
        else:
            status = "passed"
        results[node_id] = status
    return results


def run_regression_gate(
    junit_path: str | Path,
    manifest_path: str | Path,
    *,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """根据真实 JUnit 结果检查清单中的安全与可靠性场景。"""

    manifest = load_regression_manifest(manifest_path)
    test_results = _load_junit_results(junit_path)
    scenarios = manifest["required_scenarios"]
    failures: list[dict[str, str]] = []
    category_counts = {
        category: {"passed": 0, "failed": 0} for category in sorted(_CATEGORIES)
    }
    required_test_count = 0
    matched_test_count = 0

    for scenario in scenarios:
        scenario_failed = False
        for test_id in scenario["tests"]:
            required_test_count += 1
            status = test_results.get(test_id, "missing")
            if status != "missing":
                matched_test_count += 1
            if status != "passed":
                scenario_failed = True
                failures.append(
                    {
                        "scenario_id": scenario["scenario_id"],
                        "category": scenario["category"],
                        "test": test_id,
                        "status": status,
                    }
                )
        counts = category_counts[scenario["category"]]
        counts["failed" if scenario_failed else "passed"] += 1

    report = {
        "schema_version": 1,
        "passed": not failures,
        "test_count": len(test_results),
        "scenario_count": len(scenarios),
        "required_test_count": required_test_count,
        "matched_test_count": matched_test_count,
        "categories": category_counts,
        "failures": failures,
    }
    if output_path is not None:
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return report


__all__ = ["load_regression_manifest", "run_regression_gate"]
