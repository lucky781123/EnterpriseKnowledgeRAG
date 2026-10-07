"""备份恢复 plan 契约测试。"""

from enterprise_rag.infrastructure.backup import build_backup_plan


def test_backup_plan_is_non_mutating_and_contains_restore_safety():
    """计划必须明确空目标、隔离目标和恢复后验证。"""

    plan = build_backup_plan("restore", "/project/.local/backups/run-001")
    assert plan["mutated"] is False
    assert plan["safety"]["target_must_be_empty_and_isolated"] is True
    assert "restore-postgres" in plan["steps"]


def test_restore_verify_plan_requires_acl_and_replay_smoke():
    """恢复验证必须包含 ACL、查询和任务恢复后的业务 smoke。"""

    plan = build_backup_plan("restore-verify", "/project/.local/backups/run-001")
    assert "run-acl-query-replay-smoke" in plan["steps"]
