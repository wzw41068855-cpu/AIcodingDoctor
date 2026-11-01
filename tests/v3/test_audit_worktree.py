"""验证先审计后副作用、工作树归属、源仓库保护与安全拒绝。"""

import json
from pathlib import Path

import pytest

from audit.writer import AuditError, AuditWriter
from workspace.manager import git, safe_relative, WorktreeManager


def test_worktree_preserves_source_and_clean_cleanup(task):
    """实际 worktree 共享正确 Git 目录，源分支和暂存区不被切换或修改。"""
    repo, manager, _ = task
    assert git(repo, "status", "--porcelain") == ""
    assert git(manager.root, "rev-parse", "HEAD").strip() == manager.base
    assert manager.root.parent == manager.artifacts
    assert git(repo, "symbolic-ref", "HEAD").startswith("refs/heads/")
    manager.cleanup()
    assert not manager.root.exists()
    assert git(repo, "status", "--porcelain") == ""


def test_dirty_worktree_never_discarded(task):
    """AI 修改不能被任务结束清理隐式丢弃。"""
    repo, manager, _ = task
    path = manager.root / "calculator.py"
    path.write_text("def add(a, b): return a + b\n", encoding="utf-8")
    with pytest.raises(ValueError, match="保留"):
        manager.cleanup()
    assert path.exists() and "a - b" in (repo / "calculator.py").read_text(encoding="utf-8")


@pytest.mark.parametrize("tracked", [True, False])
def test_dirty_source_blocks_new_task(task, tmp_path, tracked):
    """拒绝遗漏已跟踪修改和未跟踪文件，不自动 stash 或覆盖。"""
    repo, _, _ = task
    (repo / ("calculator.py" if tracked else "new.py")).write_text("# change", encoding="utf-8")
    artifacts = tmp_path / "other"
    artifacts.mkdir()
    writer = AuditWriter(artifacts / "audit.jsonl", "task")
    manager = WorktreeManager(repo, artifacts, writer)
    with pytest.raises(ValueError, match="干净"):
        manager.create()
    assert not manager.root.exists()


@pytest.mark.parametrize("path", ["../a.py", "/a.py", "C:/a.py", "a\\b.py", ".env", "keys.py",
                                   "credentials/a.py", "a//b.py", "a.py:stream", "key.pem"])
def test_sensitive_and_escaping_paths(path):
    """在打开文件之前拒绝路径穿越、凭据和平台特殊路径。"""
    with pytest.raises(ValueError):
        safe_relative(path)


def test_audit_pre_failure_prevents_side_effect(tmp_path, monkeypatch):
    """开始事件无法 fsync 时，业务块完全不执行。"""
    import audit.writer
    writer = AuditWriter(tmp_path / "audit.jsonl", "task")
    monkeypatch.setattr(audit.writer.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError()))
    calls = []
    with pytest.raises(AuditError):
        with writer.operation("write"):
            calls.append("changed")
    assert not calls and writer.blocked


def test_audit_post_failure_retains_effect_and_latches(tmp_path, monkeypatch):
    """结束事件无法持久化时保留副作用，禁止后续操作。"""
    import audit.writer
    writer = AuditWriter(tmp_path / "audit.jsonl", "task")
    actual, count = audit.writer.os.fsync, [0]

    def fail_second(fd):
        """仅让结束事件持久化失败。"""
        count[0] += 1
        if count[0] == 2:
            raise OSError("private-secret")
        actual(fd)

    monkeypatch.setattr(audit.writer.os, "fsync", fail_second)
    output = tmp_path / "effect"
    with pytest.raises(AuditError):
        with writer.operation("write"):
            output.write_text("changed", encoding="utf-8")
    assert output.read_text(encoding="utf-8") == "changed" and writer.blocked
    with pytest.raises(AuditError):
        with writer.operation("next"):
            pytest.fail("故障后不应执行")
    assert "private-secret" not in writer.path.read_text(encoding="utf-8")


def test_audit_exception_records_type_and_duration(tmp_path):
    """业务异常仍有配对事件，取消不吞掉，正文不会进入日志。"""
    writer = AuditWriter(tmp_path / "audit.jsonl", "task")
    with pytest.raises(KeyboardInterrupt):
        with writer.operation("cancel", "Tester"):
            raise KeyboardInterrupt("private-secret")
    events = [json.loads(s) for s in writer.path.read_text(encoding="utf-8").splitlines()]
    assert events[0]["call_id"] == events[1]["call_id"]
    assert events[1]["error_type"] == "KeyboardInterrupt" and events[1]["duration_seconds"] >= 0
    assert "private-secret" not in writer.path.read_text(encoding="utf-8")


def test_git_disables_checkout_filter(task, tmp_path):
    """仓库自定义 smudge 过滤器不会在宿主 checkout 时被执行。"""
    repo, _, _ = task
    git(repo, "config", "filter.fixture.required", "true")
    git(repo, "config", "filter.fixture.smudge", "NONEXISTENT_UNSAFE_PROGRAM")
    (repo / ".gitattributes").write_text("*.py filter=fixture\n", encoding="utf-8")
    git(repo, "add", ".gitattributes")
    git(repo, "-c", "user.name=V3 Test", "-c", "user.email=v3@example.invalid", "commit", "-m", "filter")
    directory = tmp_path / "filtered"
    directory.mkdir()
    manager = WorktreeManager(repo, directory, AuditWriter(directory / "audit.jsonl", "task"))
    assert manager.create().is_dir()
