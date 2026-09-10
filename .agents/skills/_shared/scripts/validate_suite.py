#!/usr/bin/env python3
"""验证 RAG Skill suite 的结构、引用、职责和压力场景。"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError


REQUIRED_PRESSURE_CATEGORIES = {"happy_path", "boundary", "failure", "security"}
REQUIRED_CONTRACT_FIELDS = {
    "name",
    "owns",
    "not_responsible_for",
    "depends_on",
    "required_shared_contracts",
    "references",
    "scenario_files",
    "invariants",
}
PLACEHOLDER_PATTERN = re.compile(r"\b(?:TODO|TBD)\b|\[placeholder\]", re.IGNORECASE)
MARKDOWN_LINK_PATTERN = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def _read_json(path: Path, errors: list[str]) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        errors.append(f"文件不存在: {path}")
    except json.JSONDecodeError as exc:
        errors.append(f"JSON 无效: {path}: {exc}")
    return None


def _read_frontmatter(path: Path, errors: list[str]) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        errors.append(f"缺少 SKILL.md: {path}")
        return {}
    if not lines or lines[0].strip() != "---":
        errors.append(f"SKILL.md 缺少 YAML frontmatter: {path}")
        return {}
    try:
        closing = lines.index("---", 1)
    except ValueError:
        errors.append(f"SKILL.md frontmatter 未闭合: {path}")
        return {}

    values: dict[str, str] = {}
    for line in lines[1:closing]:
        if not line or line.startswith((" ", "\t")) or ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        values[key.strip()] = raw_value.strip().strip("'\"")
    return values


def _validate_markdown_links(path: Path, errors: list[str]) -> None:
    text = path.read_text(encoding="utf-8")
    for raw_target in MARKDOWN_LINK_PATTERN.findall(text):
        target = raw_target.split("#", 1)[0].strip()
        if not target or target.startswith(("http://", "https://", "mailto:")):
            continue
        if not (path.parent / target).resolve().exists():
            errors.append(f"Markdown 引用不存在: {path} -> {raw_target}")


def _validate_no_placeholders(root: Path, errors: list[str]) -> None:
    for suffix in ("*.md", "*.json", "*.yaml", "*.yml"):
        for path in root.rglob(suffix):
            if PLACEHOLDER_PATTERN.search(path.read_text(encoding="utf-8")):
                errors.append(f"发现未完成占位符: {path}")


def _validate_json_schema(path: Path, errors: list[str]) -> None:
    schema = _read_json(path, errors)
    if schema is None:
        return
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        errors.append(f"JSON Schema 无效: {path}: {exc.message}")


def validate_shared(suite_root: Path) -> list[str]:
    errors: list[str] = []
    shared_root = suite_root / "_shared"
    manifest = _read_json(shared_root / "suite-manifest.json", errors)
    if not isinstance(manifest, dict):
        return errors

    skills = manifest.get("skills")
    if not isinstance(skills, list) or not skills:
        errors.append("suite manifest 必须声明非空 skills 列表")
    contracts = manifest.get("shared_contracts")
    if not isinstance(contracts, list) or not contracts:
        errors.append("suite manifest 必须声明非空 shared_contracts 列表")
    else:
        for relative_path in contracts:
            if not isinstance(relative_path, str):
                errors.append("shared_contracts 只能包含字符串路径")
                continue
            _validate_json_schema(shared_root / relative_path, errors)

    invariants = manifest.get("global_invariants")
    if not isinstance(invariants, list) or not invariants or len(invariants) != len(set(invariants)):
        errors.append("global_invariants 必须是非空且不重复的字符串列表")

    _validate_no_placeholders(shared_root, errors)
    for path in shared_root.rglob("*.md"):
        _validate_markdown_links(path, errors)
    return errors


def _validate_skill(
    suite_root: Path,
    skill_name: str,
    manifest: dict[str, Any],
    scenario_validator: Draft202012Validator,
    responsibility_owners: dict[str, str],
    scenario_ids: set[str],
    errors: list[str],
) -> None:
    skill_root = suite_root / skill_name
    if not skill_root.is_dir():
        errors.append(f"缺少 Skill 目录: {skill_name}")
        return

    skill_file = skill_root / "SKILL.md"
    frontmatter = _read_frontmatter(skill_file, errors)
    if frontmatter.get("name") != skill_name:
        errors.append(f"SKILL.md name 与目录不一致: {skill_name}")
    if not frontmatter.get("description"):
        errors.append(f"SKILL.md description 不能为空: {skill_name}")
    if skill_file.exists():
        _validate_markdown_links(skill_file, errors)

    contract = _read_json(skill_root / "skill-contract.json", errors)
    if not isinstance(contract, dict):
        return
    missing_fields = REQUIRED_CONTRACT_FIELDS - set(contract)
    if missing_fields:
        errors.append(f"{skill_name} contract 缺少字段: {sorted(missing_fields)}")
        return
    if contract["name"] != skill_name:
        errors.append(f"skill-contract.json name 与目录不一致: {skill_name}")

    declared_skills = {item["name"] for item in manifest["skills"]}
    for dependency in contract["depends_on"]:
        if dependency not in declared_skills:
            errors.append(f"{skill_name} 依赖未声明的 Skill: {dependency}")

    for responsibility in contract["owns"]:
        previous_owner = responsibility_owners.get(responsibility)
        if previous_owner:
            errors.append(f"职责重复归属: {responsibility} 同时属于 {previous_owner} 和 {skill_name}")
        else:
            responsibility_owners[responsibility] = skill_name

    shared_root = suite_root / "_shared"
    for relative_path in contract["required_shared_contracts"]:
        if not (shared_root / relative_path).is_file():
            errors.append(f"{skill_name} 需要的共享契约不存在: {relative_path}")
    for relative_path in contract["references"]:
        if not (skill_root / relative_path).is_file():
            errors.append(f"{skill_name} reference 不存在: {relative_path}")

    allowed_invariants = set(manifest["global_invariants"]) | set(contract["invariants"])
    observed_categories: set[str] = set()
    for relative_path in contract["scenario_files"]:
        scenario_path = skill_root / relative_path
        scenarios = _read_json(scenario_path, errors)
        if scenarios is None:
            continue
        for violation in scenario_validator.iter_errors(scenarios):
            location = "/".join(str(part) for part in violation.absolute_path)
            errors.append(f"场景 schema 不通过: {scenario_path}:{location}: {violation.message}")
        if not isinstance(scenarios, list):
            continue
        for scenario in scenarios:
            if not isinstance(scenario, dict):
                continue
            scenario_id = scenario.get("id")
            if scenario_id in scenario_ids:
                errors.append(f"场景 ID 重复: {scenario_id}")
            elif isinstance(scenario_id, str):
                scenario_ids.add(scenario_id)
            if scenario.get("skill") != skill_name:
                errors.append(f"场景归属错误: {scenario_id} 应属于 {skill_name}")
            category = scenario.get("category")
            if isinstance(category, str):
                observed_categories.add(category)
            unknown_invariants = set(scenario.get("required_invariants", [])) - allowed_invariants
            if unknown_invariants:
                errors.append(f"场景引用未声明不变量: {scenario_id}: {sorted(unknown_invariants)}")

    missing_categories = REQUIRED_PRESSURE_CATEGORIES - observed_categories
    if missing_categories:
        errors.append(f"{skill_name} 缺少压力类别: {sorted(missing_categories)}")

    _validate_no_placeholders(skill_root, errors)
    for path in skill_root.rglob("*.md"):
        _validate_markdown_links(path, errors)


def validate_suite(suite_root: Path) -> list[str]:
    suite_root = Path(suite_root)
    errors = validate_shared(suite_root)
    manifest = _read_json(suite_root / "_shared/suite-manifest.json", errors)
    scenario_schema = _read_json(suite_root / "_shared/contracts/skill-scenario.schema.json", errors)
    if not isinstance(manifest, dict) or not isinstance(scenario_schema, dict):
        return errors

    skills = manifest.get("skills", [])
    if not all(isinstance(item, dict) and "name" in item and "order" in item for item in skills):
        errors.append("suite manifest 中每个 Skill 必须声明 name 和 order")
        return errors
    names = [item["name"] for item in sorted(skills, key=lambda item: item["order"])]
    if len(names) != len(set(names)):
        errors.append("suite manifest 中 Skill 名称重复")
    if sorted(item["order"] for item in skills) != list(range(1, len(skills) + 1)):
        errors.append("suite manifest 中 Skill order 必须从 1 连续递增")

    scenario_validator = Draft202012Validator(scenario_schema)
    responsibility_owners: dict[str, str] = {}
    scenario_ids: set[str] = set()
    for skill_name in names:
        _validate_skill(
            suite_root,
            skill_name,
            manifest,
            scenario_validator,
            responsibility_owners,
            scenario_ids,
            errors,
        )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "suite_root",
        nargs="?",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="`.agents/skills` 目录，默认自动定位当前 suite",
    )
    parser.add_argument("--shared-only", action="store_true", help="只验证共享层")
    args = parser.parse_args()
    errors = validate_shared(args.suite_root) if args.shared_only else validate_suite(args.suite_root)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("RAG Skill suite validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
