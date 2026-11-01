"""V2 编辑器离线安全与原子提交回归，只使用 pytest 隔离副本。"""

import os
from pathlib import Path

import pytest

import config
from graph.editor import Editor, Workspace, sha256
from graph.state import EditBatch


@pytest.fixture
def editor(tmp_path):
    """为每个用例创建全新的可信副本编辑器。"""
    root = tmp_path / "copy"
    root.mkdir()
    (root / "a.py").write_bytes(b"x = 1\n")
    (root / "b.py").write_bytes(b"y = 1\n")
    return Editor(Workspace(root, trusted_copy=True))


def batch(editor, path="a.py", old="1", new="2", digest=None):
    """根据当前原始字节生成测试替换块。"""
    target = editor.workspace.root / path
    return EditBatch(edits=[{"path": path, "old_text": old, "new_text": new,
                              "expected_hash": digest or sha256(target.read_bytes())}])


def test_edit_snapshot_diff_and_newline(editor):
    """真实读取、写入和 Diff 保留中文、声明编码与 CRLF。"""
    target = editor.workspace.root / "a.py"
    target.write_bytes('# coding: utf-8\r\nx = "中文1"\r\n'.encode())
    assert editor.snapshot("a.py")["content"].endswith('x = "中文1"\n')
    result = editor.apply(batch(editor), ["a.py"])
    assert result[0].applied and result[0].before_hash != result[0].after_hash
    assert b"\r\n" in target.read_bytes()
    assert '-x = "中文1"' in editor.diff() and '+x = "中文2"' in editor.diff()
    assert "中文2" in editor.snapshot("a.py")["content"]


def test_diff_for_file_without_final_newline(editor):
    """文件无末尾换行时 Diff 仍分开呈现新旧内容并明确标记。"""
    (editor.workspace.root / "a.py").write_bytes(b"x = 1")
    editor.apply(batch(editor), ["a.py"])
    assert "-x = 1\n\\ No newline at end of file\n+x = 2\n" in editor.diff()


@pytest.mark.parametrize("old,new", [("missing", "2"), ("1", "1"), ("1", "(")])
def test_invalid_replacement_keeps_original(editor, old, new):
    """无匹配、无变化和语法错误都不写入。"""
    with pytest.raises((ValueError, SyntaxError)):
        editor.apply(batch(editor, old=old, new=new), ["a.py"])
    assert (editor.workspace.root / "a.py").read_bytes() == b"x = 1\n"


def test_hash_unique_empty_whitelist_and_duplicate(editor):
    """拒绝旧版本、非唯一匹配、空补丁、越过计划和同文件重复块。"""
    with pytest.raises(ValueError):
        editor.apply(batch(editor, digest="0" * 64), ["a.py"])
    (editor.workspace.root / "a.py").write_text("x = 1 + 1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        editor.apply(batch(editor), ["a.py"])
    with pytest.raises(ValueError):
        EditBatch(edits=[])
    with pytest.raises(ValueError):
        editor.apply(batch(editor, old="1 + 1"), [])
    item = batch(editor, old="1 + 1").edits[0]
    with pytest.raises(ValueError):
        editor.apply(EditBatch(edits=[item, item]), ["a.py"])


@pytest.mark.parametrize("path", ["../outside.py", ".git/a.py", ".env", "C:\\a.py", "/a.py",
                                  "a.py:stream", "tests/test_a.py", "conftest.py", "setup.py",
                                  "credentials.py", "config.py", "settings.py", "keys.py", "new.py"])
def test_reject_paths(editor, path):
    """拒绝路径穿越、绝对路径、敏感文件、测试和新建文件。"""
    with pytest.raises((ValueError, OSError)):
        editor.workspace.path(path)


def test_workspace_authorization_and_project_protection(tmp_path):
    """拒绝未声明信任、开发仓库及其父目录，并验证字节上限。"""
    for path, trusted in [(tmp_path, False), (config.BASE_DIR, True), (config.BASE_DIR.parent, True)]:
        with pytest.raises(ValueError):
            Workspace(path, trusted_copy=trusted)
    with pytest.raises(ValueError):
        Workspace(tmp_path, trusted_copy=True, max_bytes=0)


@pytest.mark.parametrize("raw", [b"x\x00y", b"#" * 65537, b"#\n" * 1001], ids=["binary", "large", "long"])
def test_binary_large_and_long_file(editor, raw):
    """二进制、超字节限制和超行数都不能进入模型全文证据。"""
    (editor.workspace.root / "a.py").write_bytes(raw)
    with pytest.raises(ValueError):
        editor.snapshot("a.py")


def test_batch_validation_before_commit_and_rollback(editor, monkeypatch):
    """后一个文件验证失败不写前一个；第二次替换失败恢复第一文件。"""
    first = batch(editor).edits[0]
    second = batch(editor, "b.py").edits[0]
    bad = second.model_copy(update={"expected_hash": "0" * 64})
    with pytest.raises(ValueError):
        editor.apply(EditBatch(edits=[first, bad]), ["a.py", "b.py"])
    replace = os.replace
    calls = []

    def fail_second(source, target):
        calls.append(target)
        if len(calls) == 2:
            raise OSError("模拟第二文件提交失败")
        return replace(source, target)

    monkeypatch.setattr(os, "replace", fail_second)
    with pytest.raises(OSError):
        editor.apply(EditBatch(edits=[first, second]), ["a.py", "b.py"])
    assert (editor.workspace.root / "a.py").read_bytes() == b"x = 1\n"
    assert (editor.workspace.root / "b.py").read_bytes() == b"y = 1\n"
    assert not list(editor.workspace.root.glob(".v2-edit-*"))


def test_link_checks_without_symlink_privilege(editor, monkeypatch):
    """即使系统未授权创建链接，也验证路径逐级链接拒绝逻辑。"""
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda self: self.name == "a.py" or original(self))
    with pytest.raises(ValueError):
        editor.snapshot("a.py")


def test_real_symlink_escape(editor, tmp_path):
    """系统支持时用真实越界链接验证；不支持则明确跳过。"""
    outside = tmp_path / "outside.py"
    outside.write_text("x = 99\n", encoding="utf-8")
    try:
        (editor.workspace.root / "link.py").symlink_to(outside)
    except OSError:
        pytest.skip("系统未授予创建符号链接权限")
    with pytest.raises(ValueError):
        editor.snapshot("link.py")


def test_hardlink_and_current_change_records(editor, tmp_path):
    """硬链接不能绕过副本边界；最终变更按任务起点报告。"""
    outside = tmp_path / "outside.py"
    outside.write_text("x = 9\n", encoding="utf-8")
    link = editor.workspace.root / "hard.py"
    os.link(outside, link)
    with pytest.raises(ValueError):
        editor.snapshot("hard.py")
    editor.apply(batch(editor), ["a.py"])
    assert editor.current_changes()[0].path == "a.py"
    editor.apply(batch(editor, old="2", new="1"), ["a.py"])
    assert editor.current_changes() == []


def test_rollback_failure_preserves_reviewable_baseline(editor, monkeypatch):
    """无法回滚时明确报错并保留任务基线，供最终报告显示残留修改。"""
    first, second = batch(editor).edits[0], batch(editor, "b.py").edits[0]
    replace, write = os.replace, Path.write_bytes

    def fail_second(source, target):
        if target.name == "b.py":
            raise OSError("提交失败")
        return replace(source, target)

    def fail_restore(path, raw):
        if path.name == "a.py":
            raise OSError("恢复失败")
        return write(path, raw)

    monkeypatch.setattr(os, "replace", fail_second)
    monkeypatch.setattr(Path, "write_bytes", fail_restore)
    with pytest.raises(RuntimeError):
        editor.apply(EditBatch(edits=[first, second]), ["a.py", "b.py"])
    assert editor.current_changes()[0].path == "a.py"
    assert "+x = 2" in editor.diff()
