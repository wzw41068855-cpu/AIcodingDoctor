"""任务诊断必须说明安全业务原因，同时避免泄露异常中的密钥和源码。"""

import json

from web_runtime.diagnostics import record_failure


def test_dirty_repository_reason_and_stack(tmp_path):
    """已知源仓库非干净错误显示解决提示，并记录安全栈位置。"""
    try:
        raise ValueError("源仓库必须干净；不会忽略未提交或未跟踪文件")
    except ValueError as exc:
        result = record_failure(tmp_path, exc, "创建隔离 Git worktree")
    assert "git status" in result["error"]
    saved = json.loads((tmp_path / "diagnostics.jsonl").read_text(encoding="utf-8"))
    assert saved["frames"][-1]["function"] == "test_dirty_repository_reason_and_stack"
    assert saved["stage"] == "创建隔离 Git worktree"


def test_unknown_exception_never_records_message(tmp_path):
    """SDK 原文、未知 ValueError 和局部变量不会进入诊断。"""
    secret = "fixture-private-key"
    for error in (RuntimeError(secret), ValueError(secret)):
        result = record_failure(tmp_path, error, "初始化模型")
        assert secret not in json.dumps(result)
    assert secret not in (tmp_path / "diagnostics.jsonl").read_text(encoding="utf-8")


def test_diagnostic_write_failure_preserves_original_error(tmp_path):
    """日志无法创建时，调用者仍能将原始诊断保存到任务数据库。"""
    result = record_failure(tmp_path / "missing", ValueError("private"), "任务初始化")
    assert result["error_type"] == "ValueError"
    assert result["diagnostic_error"]


def test_exception_group_records_inner_failure_without_secrets(tmp_path):
    """MCP 会话包装的异常组保留内部异常类型，不记录异常原文。"""
    try:
        try:
            raise TypeError("fixture-private-key")
        except TypeError as exc:
            raise ExceptionGroup("private outer", [exc]) from None
    except ExceptionGroup as exc:
        result = record_failure(tmp_path, exc, "保存节点结果")
    children = result["diagnostics"]["children"]
    assert children[0]["error_type"] == "TypeError"
    assert children[0]["frames"]
    assert "fixture-private-key" not in json.dumps(result)
