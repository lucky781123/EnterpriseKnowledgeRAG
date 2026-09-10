from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_SUITE_ROOT = REPO_ROOT / ".agents/skills"
VALIDATOR_PATH = SKILL_SUITE_ROOT / "_shared/scripts/validate_suite.py"
SKILL_NAMES = [
    "retrieval-pipeline-engineering",
    "rag-acl-multitenancy-security",
    "fastapi-model-provider-service",
    "rag-degradation-and-fallback",
    "deepeval-rag-evaluation",
]
PRESSURE_CATEGORIES = ["happy_path", "boundary", "failure", "security"]


def _load_validator() -> ModuleType:
    assert VALIDATOR_PATH.exists(), f"缺少 Skill suite 验证器: {VALIDATOR_PATH}"
    spec = importlib.util.spec_from_file_location("skill_suite_validator", VALIDATOR_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _build_minimal_suite(root: Path) -> Path:
    suite_root = root / ".agents/skills"
    shared_root = suite_root / "_shared"

    manifest = {
        "schema_version": "1.0",
        "skills": [{"name": name, "order": index + 1} for index, name in enumerate(SKILL_NAMES)],
        "shared_contracts": [
            "contracts/request-context.schema.json",
            "contracts/skill-scenario.schema.json",
        ],
        "global_invariants": ["tenant-context-required"],
    }
    _write_json(shared_root / "suite-manifest.json", manifest)
    _write_json(
        shared_root / "contracts/request-context.schema.json",
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://example.test/request-context.schema.json",
            "type": "object",
            "required": ["request_id"],
            "properties": {"request_id": {"type": "string", "minLength": 1}},
            "additionalProperties": False,
        },
    )
    _write_json(
        shared_root / "contracts/skill-scenario.schema.json",
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://example.test/skill-scenario.schema.json",
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "id",
                    "skill",
                    "category",
                    "request",
                    "setup",
                    "expected_decisions",
                    "required_invariants",
                    "forbidden_actions",
                    "verification",
                ],
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "skill": {"type": "string", "minLength": 1},
                    "category": {"enum": PRESSURE_CATEGORIES},
                    "request": {"type": "string", "minLength": 1},
                    "setup": {"type": "array", "minItems": 1},
                    "expected_decisions": {"type": "array", "minItems": 1},
                    "required_invariants": {"type": "array", "minItems": 1},
                    "forbidden_actions": {"type": "array", "minItems": 1},
                    "verification": {"type": "array", "minItems": 1},
                },
                "additionalProperties": False,
            },
        },
    )

    for index, skill_name in enumerate(SKILL_NAMES):
        skill_root = suite_root / skill_name
        responsibility = f"responsibility-{index + 1}"
        previous_skill = SKILL_NAMES[index - 1] if index else None
        dependencies = [previous_skill] if previous_skill else []
        (skill_root / "references").mkdir(parents=True)
        (skill_root / "tests").mkdir(parents=True)
        (skill_root / "SKILL.md").write_text(
            "\n".join(
                [
                    "---",
                    f"name: {skill_name}",
                    f"description: Use when handling {skill_name} tasks.",
                    "---",
                    "",
                    f"# {skill_name}",
                    "",
                    "Read [the guide](references/guide.md).",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (skill_root / "references/guide.md").write_text("# Guide\n", encoding="utf-8")
        _write_json(
            skill_root / "skill-contract.json",
            {
                "name": skill_name,
                "owns": [responsibility],
                "not_responsible_for": [],
                "depends_on": dependencies,
                "required_shared_contracts": ["contracts/request-context.schema.json"],
                "references": ["references/guide.md"],
                "scenario_files": ["tests/scenarios.json"],
                "invariants": [responsibility],
            },
        )
        scenarios = [
            {
                "id": f"{skill_name}-{category}",
                "skill": skill_name,
                "category": category,
                "request": f"处理 {category} 场景",
                "setup": ["准备最小输入"],
                "expected_decisions": ["保持契约"],
                "required_invariants": ["tenant-context-required", responsibility],
                "forbidden_actions": ["跳过验证"],
                "verification": ["检查结构化结果"],
            }
            for category in PRESSURE_CATEGORIES
        ]
        _write_json(skill_root / "tests/scenarios.json", scenarios)

    return suite_root


def test_validator_accepts_a_complete_minimal_suite(tmp_path: Path) -> None:
    validator = _load_validator()
    suite_root = _build_minimal_suite(tmp_path)

    assert validator.validate_suite(suite_root) == []


def test_repository_suite_declares_requested_skills_in_order() -> None:
    manifest = json.loads(
        (SKILL_SUITE_ROOT / "_shared/suite-manifest.json").read_text(encoding="utf-8")
    )
    skill_names = [item["name"] for item in sorted(manifest["skills"], key=lambda item: item["order"])]

    assert skill_names == SKILL_NAMES


def test_repository_skill_suite_is_valid() -> None:
    validator = _load_validator()

    assert validator.validate_suite(SKILL_SUITE_ROOT) == []


def test_validator_rejects_duplicate_responsibility_owners(tmp_path: Path) -> None:
    validator = _load_validator()
    suite_root = _build_minimal_suite(tmp_path)
    first_contract = json.loads(
        (suite_root / SKILL_NAMES[0] / "skill-contract.json").read_text(encoding="utf-8")
    )
    second_contract_path = suite_root / SKILL_NAMES[1] / "skill-contract.json"
    second_contract = json.loads(second_contract_path.read_text(encoding="utf-8"))
    second_contract["owns"] = first_contract["owns"]
    _write_json(second_contract_path, second_contract)

    errors = validator.validate_suite(suite_root)

    assert any("职责" in error and "重复" in error for error in errors)


def test_validator_rejects_broken_markdown_links(tmp_path: Path) -> None:
    validator = _load_validator()
    suite_root = _build_minimal_suite(tmp_path)
    skill_file = suite_root / SKILL_NAMES[0] / "SKILL.md"
    skill_file.write_text(
        skill_file.read_text(encoding="utf-8")
        + "Read [missing guidance](references/missing.md).\n",
        encoding="utf-8",
    )

    errors = validator.validate_suite(suite_root)

    assert any("引用不存在" in error for error in errors)


def test_validator_requires_all_pressure_categories(tmp_path: Path) -> None:
    validator = _load_validator()
    suite_root = _build_minimal_suite(tmp_path)
    scenario_path = suite_root / SKILL_NAMES[0] / "tests/scenarios.json"
    scenarios = json.loads(scenario_path.read_text(encoding="utf-8"))
    _write_json(
        scenario_path,
        [scenario for scenario in scenarios if scenario["category"] != "security"],
    )

    errors = validator.validate_suite(suite_root)

    assert any("缺少压力类别" in error and "security" in error for error in errors)


@pytest.mark.parametrize("placeholder", ["TODO", "TBD", "[placeholder]"])
def test_validator_rejects_unfinished_placeholders(tmp_path: Path, placeholder: str) -> None:
    validator = _load_validator()
    suite_root = _build_minimal_suite(tmp_path)
    guide_path = suite_root / SKILL_NAMES[0] / "references/guide.md"
    guide_path.write_text(f"# Guide\n\n{placeholder}\n", encoding="utf-8")

    errors = validator.validate_suite(suite_root)

    assert any("未完成占位符" in error for error in errors)
