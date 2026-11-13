"""长上下文、消息配对、恢复及项目经验隔离。"""

import json
from pathlib import Path
import pytest
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage

from memory.context import ContextBuilder
from memory.experience import ExperienceStore
from memory.messages import trim_dialogue, validate_pairs


def test_twenty_turns_and_thirty_large_results(tmp_path: Path) -> None:
    """预算至少超三倍仍保留目标、授权、最新失败和全部产物引用。"""
    builder = ContextBuilder(tmp_path, counter=len, input_tokens=1800)
    history = []
    for i in range(30):
        result = {"relative_path": f"file{i}.py", "start_line": 1, "end_line": 3,
                  "source_version": str(i), "content": "large-tool-result " * 1000}
        history.append({"tool": "read_file", "result": builder.observe("read_file", result)})
    evidence = [{"role": "user", "summary": str(i) + "long-conversation " * 100} for i in range(20)]
    payload = {"request": "只修改授权文件", "allowed_files": ["main.py"], "tests": {"outcome": "failed", "output": "latest failure"},
               "patch_state": "waiting approval", "history": history, "evidence": evidence}
    calls = []
    result = builder.build("Coder", payload, lambda text: calls.append(text) or "旧结论：需要修复；权限不变")
    assert len(json.dumps(result, ensure_ascii=False)) <= 1800
    assert result["request"] == payload["request"] and result["tests"]["output"] == "latest failure"
    assert result["allowed_files"] == ["main.py"] and result["patch_state"] == "waiting approval"
    state = ContextBuilder(tmp_path).load()
    assert len(state["facts"]) == len(state["tools"]) == 30 and calls
    assert builder.artifacts.read(state["tools"][0]["artifact_id"])["content"]
    assert builder.load()["compressed"] == 1


def test_summary_failure_and_missing_artifact(tmp_path: Path) -> None:
    """摘要失败确定性降级，引用缺失不补造内容。"""
    builder = ContextBuilder(tmp_path, counter=len, input_tokens=900)
    def failed(text: str) -> str:
        raise RuntimeError("unavailable")
    result = builder.build("Explorer", {"request": "目标", "evidence": [{"summary": "old" * 200}] * 20}, failed)
    assert len(json.dumps(result, ensure_ascii=False)) <= 900
    with pytest.raises(FileNotFoundError):
        builder.artifacts.read("f" * 64)
    with pytest.raises(ValueError):
        builder.artifacts.read("../escape")
    with pytest.raises(ValueError):
        builder.build("Coder", {"request": "x" * 4000})


def test_native_message_pairs() -> None:
    """官方裁剪器保留完整工具组，非法输入拒绝。"""
    messages = [HumanMessage(content="question"), AIMessage(content="", tool_calls=[{"id": "one", "name": "read_file", "args": {}}]),
                ToolMessage(content="result", tool_call_id="one"), AIMessage(content="answer")]
    validate_pairs(trim_dialogue(messages, 1000))
    with pytest.raises(ValueError):
        validate_pairs(messages[2:])
    with pytest.raises(ValueError):
        validate_pairs(messages[:2])


def test_experience_explicit_crud_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """显式保存、更新、删除即时生效，项目和凭据保持隔离。"""
    store = ExperienceStore(tmp_path / "experiences.sqlite")
    with pytest.raises(ValueError):
        store.save("repo1", "约定", "test", explicit=False)
    assert store.listing("repo1") == []
    key = store.save("repo1", "分页使用 page_size", "user", explicit=True)
    assert store.retrieve("repo1", "分页")[0]["id"] == key
    assert store.retrieve("repo2", "分页") == []
    store.save("repo1", "分页保持原接口", "updated", explicit=True, experience_id=key)
    assert "原接口" in store.retrieve("repo1", "分页")[0]["content"]
    assert not store.delete("repo2", key)
    assert store.delete("repo1", key) and store.retrieve("repo1", "分页") == []
    monkeypatch.setenv("AICD_WEB_API_KEY", "sentinel-secret-value")
    with pytest.raises(ValueError):
        store.save("repo1", "sentinel-secret-value", "user", explicit=True)
