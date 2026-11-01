"""应用已验收修改时保护源仓库、用户改动和重复请求。"""

import pytest

from graph.editor import sha256
from web_runtime.apply_changes import prepare_application, apply_application
from workspace.manager import git


def ready(task):
    """构造已验收的工作树修复，原仓库保持干净。"""
    repo, manager, config = task
    path = manager.root / "calculator.py"
    before = path.read_bytes()
    after = before.replace(b"return a - b", b"return a + b")
    path.write_bytes(after)
    (manager.artifacts / "task.json").write_text(config.model_dump_json(), encoding="utf-8")
    view = {"id": config.task_id, "status": "success", "options": {"repository": str(repo), "mode": "repair"},
        "result": {"final_report": {"status": "success", "tests": {"outcome": "passed", "returncode": 0, "passed": 1},
            "changes": [{"path": "calculator.py", "before_hash": sha256(before), "after_hash": sha256(after)}]}}}
    return repo, manager, view, before, after


def test_apply_verified_changes_without_commit(task):
    """应用正确字节，保留 HEAD，原仓库只产生未暂存修改。"""
    repo, manager, view, before, after = ready(task)
    head = git(repo, "rev-parse", "HEAD")
    editor, batch, preview = prepare_application(manager.artifacts, view)
    assert preview["files"] == ["calculator.py"]
    assert (repo / "calculator.py").read_bytes() == before
    records = apply_application(editor, batch)
    assert (repo / "calculator.py").read_bytes() == after
    assert records[0]["after_hash"] == sha256(after)
    assert git(repo, "rev-parse", "HEAD") == head
    assert not git(repo, "diff", "--cached", "--name-only")


@pytest.mark.parametrize("case", ["dirty", "tampered", "blocked", "path_escape", "no_tests"])
def test_apply_rejects_unverified_changes(task, case):
    """源仓库有改动、修复被篡改或报告无效时，不执行任何写回。"""
    repo, manager, view, before, after = ready(task)
    if case == "dirty":
        (repo / "user.txt").write_text("keep", encoding="utf-8")
    elif case == "tampered":
        (manager.root / "calculator.py").write_text("changed", encoding="utf-8")
    elif case == "blocked":
        view["status"] = "blocked"
    elif case == "path_escape":
        view["result"]["final_report"]["changes"][0]["path"] = "../escape.py"
    else:
        view["result"]["final_report"]["tests"]["passed"] = 0
    with pytest.raises(ValueError):
        prepare_application(manager.artifacts, view)
    assert (repo / "calculator.py").read_bytes() == before


def test_apply_rechecks_after_preview(task):
    """用户确认期间修改了源码，Editor 哈希校验拒绝覆盖。"""
    repo, manager, view, before, after = ready(task)
    editor, batch, _ = prepare_application(manager.artifacts, view)
    user_content = before + b"\n# user edit\n"
    (repo / "calculator.py").write_bytes(user_content)
    with pytest.raises(ValueError):
        apply_application(editor, batch)
    assert (repo / "calculator.py").read_bytes() == user_content
