"""分别初始化只读问答和修复任务，副作用先记账。"""

import json
from pathlib import Path

from audit.writer import AuditWriter
from graph.editor import Workspace
from graph.operations import OperationJournal
from mcp_servers.context import TaskConfig
from services.code_search import CodeSearchService
from workspace.manager import WorktreeManager, git
from security.path_policy import reject_links


def initialize(directory: Path, options: dict) -> TaskConfig:
    """恢复冻结配置，或按模式首次初始化。

    Args:
        directory: 工作树外任务目录。
        options: 不含凭据的用户授权参数。
    Returns:
        已冻结的 MCP 配置。
    """
    target = directory / "task.json"
    if target.exists():
        return TaskConfig.model_validate_json(target.read_text(encoding="utf-8"))
    repository = Path(options["repository"]).resolve(strict=True)
    reject_links(repository)
    if not options["trusted_copy"] or Path(git(repository, "rev-parse", "--show-toplevel").strip()).resolve() != repository:
        raise ValueError("必须授权可信 Git 仓库根目录")
    readonly = options["mode"] == "ask"
    if readonly:
        # 只读入口不依赖 Workspace 的编辑边界，不构建 Worktree 和 DockerRunner。
        CodeSearchService(repository)
        root, base, image, allowed, tests = repository, git(repository, "rev-parse", "HEAD").strip(), "", [], []
    else:
        from sandbox.docker_runner import DockerRunner
        manager = WorktreeManager(repository, directory, AuditWriter(directory / "audit.jsonl", directory.name))
        journal = OperationJournal(directory / "operations.sqlite")

        def create() -> dict:
            """创建工作树并返回可序列化结果。"""
            path = manager.create()
            return {"root": str(path), "base": manager.base}

        def reconcile() -> dict | None:
            """只确认完整的已有工作树；半创建状态阻塞。"""
            marker = directory / "worktree.json"
            if not marker.exists():
                return None
            data = json.loads(marker.read_text(encoding="utf-8"))
            if (Path(data["repository"]).resolve() == repository and Path(data["root"]).resolve() == manager.root
                    and git(manager.root, "rev-parse", "HEAD").strip() == data["base"]):
                return {"root": str(manager.root), "base": data["base"]}
            return None

        created = journal.run("worktree.create", {"repository": str(repository)}, create, reconcile)
        root, base = Path(created["root"]), created["base"]
        workspace = Workspace(root, trusted_copy=True)
        tests = options["tests"]
        allowed = options["allowed_files"]
        if not allowed:
            from web_runtime.options import repository_options
            allowed = repository_options(root)["allowed_files"]
        runner = DockerRunner(workspace, tests, options["image"], timeout=options["timeout"],
                              task_id=directory.name, artifacts=directory)
        image = runner.preflight()
    task = TaskConfig(task_id=directory.name, workspace=str(root), artifacts=str(directory),
        base_commit=base, allowed_files=allowed, tests=tests, image=image,
        timeout=options["timeout"], trusted_copy=True, readonly=readonly, enforce_roles=True,
        allow_regression_tests=options.get("allow_regression_tests", False))
    target.write_text(task.model_dump_json(), encoding="utf-8")
    return task
