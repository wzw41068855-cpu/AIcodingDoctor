"""将已验收的任务修改应用到绑定源仓库，不执行 Git 提交。"""

import json
import io
from pathlib import Path
import tokenize

from graph.editor import Editor, Workspace, sha256
from graph.state import ChangeRecord, EditBatch, TextEdit
from mcp_servers.context import TaskConfig
from workspace.manager import git


def prepare_application(directory: Path, view: dict):
    """核对任务归属、Git 基线和文件哈希，生成确定性整文件替换。

    Args:
        directory: 服务端绑定的任务目录，不接受浏览器传入仓库路径。
        view: 持久任务视图，必须为成功的修复任务。
    Returns:
        源仓库 Editor、已验证 EditBatch 和供确认页面显示的预览。
    Raises:
        ValueError: 任务未成功、工作区归属不符、仓库有修改或文件哈希冲突。
        OSError: 任务文件或源码不可访问。
        RuntimeError: Git 检查失败。
    Notes:
        仅支持既有业务 Python 文件；沿用 Editor 的路径、语法、版本、
        批量预检和失败回滚机制，不允许应用测试、配置或任意补丁。
    """
    if view["status"] != "success" or view["options"]["mode"] != "repair":
        raise ValueError("仅成功的修复任务可以应用修改")
    task = TaskConfig.model_validate_json((directory / "task.json").read_text(encoding="utf-8"))
    marker = json.loads((directory / "worktree.json").read_text(encoding="utf-8"))
    repository = Path(view["options"]["repository"]).resolve(strict=True)
    if (task.task_id != view["id"] or Path(task.artifacts).resolve() != directory.resolve()
            or Path(task.workspace).resolve() != directory.resolve() / "worktree"
            or Path(marker["repository"]).resolve() != repository
            or marker["base"] != task.base_commit
            or Path(marker["root"]).resolve() != Path(task.workspace).resolve()):
        raise ValueError("任务与源仓库归属不符")
    source = Workspace(repository, trusted_copy=True)
    repaired = Workspace(task.workspace, trusted_copy=True)
    if Path(git(repository, "rev-parse", "--show-toplevel").strip()).resolve() != repository:
        raise ValueError("源仓库根目录不匹配")
    if git(repository, "rev-parse", "HEAD").strip() != task.base_commit:
        raise ValueError("源仓库提交已改变，请重新执行修复任务")
    if git(repository, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("源仓库有未提交或未跟踪文件，请先处理后再应用")
    result = view.get("result", {})
    report = result.get("final_report", {})
    tests = report.get("tests") or {}
    if (report.get("status") != "success" or tests.get("outcome") != "passed"
            or tests.get("returncode") != 0 or tests.get("passed", 0) < 1):
        raise ValueError("缺少成功的最终验收报告")
    changes = [ChangeRecord.model_validate(record) for record in report.get("changes", [])]
    if not changes or len(changes) > 8 or len({c.path for c in changes}) != len(changes):
        raise ValueError("修改清单为空、重复或超过上限")
    editor = Editor(source)
    edits = []
    for record in changes:
        if record.path not in task.allowed_files:
            raise ValueError("修改超出授权文件范围")
        before = editor.snapshot(record.path)
        repaired_path = repaired.path(record.path)
        if repaired_path.stat().st_size > repaired.max_bytes:
            raise ValueError("修复文件超过读取上限")
        after = repaired_path.read_bytes()
        if before["expected_hash"] != record.before_hash or sha256(after) != record.after_hash:
            raise ValueError("源文件或修复文件已变化，不能覆盖或应用未验收内容")
        content = repaired.service.get_code_context(record.path).content
        original = source.path(record.path).read_bytes()
        encoding = tokenize.detect_encoding(io.BytesIO(original).readline)[0]
        newline = "\r\n" if b"\r\n" in original else "\n"
        if sha256(content.replace("\n", newline).encode(encoding)) != record.after_hash:
            raise ValueError("源文件编码或换行与已验收修改不兼容")
        # 预览和正式应用均重新读取磁盘；Editor 再次检查 expected_hash，
        # 防止等待用户确认期间的外部修改被覆盖。
        edits.append(TextEdit(path=record.path, expected_hash=record.before_hash,
                              old_text=before["content"], new_text=content))
    preview = {"repository": str(repository), "workspace": task.workspace,
               "files": [c.path for c in changes], "base_commit": task.base_commit,
               "note": "写回原仓库工作区，不创建提交；请先审查网页 Git Diff"}
    return editor, EditBatch(edits=edits), preview


def apply_application(editor: Editor, batch: EditBatch) -> list[dict]:
    """应用已准备的替换批次，复用 Editor 的完整校验及失败回滚。

    Args:
        editor: 已绑定并授权源仓库的编辑器。
        batch: prepare_application 生成的确定性替换。
    Returns:
        实际修改的路径和前后哈希列表；不暂存、不提交源码。
    Raises:
        ValueError: 源文件并发变化、补丁无效或语法不合法。
        OSError: 写入失败，编辑器会尝试回滚。
        RuntimeError: 回滚失败，需要人工检查原仓库。
    """
    return [record.model_dump() for record in editor.apply(batch, [e.path for e in batch.edits])]
