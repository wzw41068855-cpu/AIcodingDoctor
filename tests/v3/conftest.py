"""只在临时仓库创建 worktree，模型和索引默认使用离线替身。"""

from pathlib import Path
from uuid import uuid4

import pytest

from audit.writer import AuditWriter
from graph.demo import create_demo
from mcp_servers.context import TaskConfig
from workspace.manager import git, WorktreeManager


@pytest.fixture
def task(tmp_path):
    """生成已提交的可信 calculator 仓库及独立任务 worktree。"""
    repo = tmp_path / "repository"
    create_demo(repo)
    git(repo, "init")
    git(repo, "add", "--", "calculator.py", "tests/test_calculator.py")
    git(repo, "-c", "user.name=V3 Test", "-c", "user.email=v3@example.invalid", "commit", "-m", "fixture")
    artifacts = tmp_path / "task"
    artifacts.mkdir()
    task_id = uuid4().hex
    audit = AuditWriter(artifacts / "audit.jsonl", task_id)
    manager = WorktreeManager(repo, artifacts, audit)
    root = manager.create()
    config = TaskConfig(task_id=task_id, workspace=str(root), artifacts=str(artifacts),
                        base_commit=manager.base, allowed_files=["calculator.py"],
                        tests=["tests/test_calculator.py"], image="fixture:local", trusted_copy=True,
                        mock_index=True)
    return repo, manager, config
