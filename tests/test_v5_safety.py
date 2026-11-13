"""V5 审批、操作对账、状态竞争和凭据分域回归。"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3

import pytest

from graph.editor import Editor, Workspace, sha256
from graph.operations import OperationJournal
from graph.state import EditBatch
from security.credentials import credential
from web_runtime.store import Store


def test_cancel_progress_race(tmp_path: Path) -> None:
    """重复竞争时，进度不能复活已取消任务。"""
    store = Store(tmp_path / "tasks.sqlite")
    for i in range(20):
        key = str(i)
        store.create(key, {})
        store.claim(key, "queued")
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(store.update, key, "cancelling"),
                       pool.submit(store.update, key, "running", progress="late")]
            for future in futures:
                future.result()
        assert store.get(key)["status"] == "cancelling"
        assert not store.update(key, "success")
        store.update(key, "cancelled")
        assert not store.update(key, "running")


def test_patch_preview_and_stale_approval(tmp_path: Path) -> None:
    """审批预览不写文件，源版本改变后拒绝实际应用。"""
    root = tmp_path / "repo"
    root.mkdir()
    path = root / "main.py"
    path.write_bytes(b"def add(a, b):\n    return a - b\n")
    editor = Editor(Workspace(root, trusted_copy=True))
    batch = EditBatch(edits=[{"path": "main.py", "expected_hash": sha256(path.read_bytes()),
        "old_text": "return a - b", "new_text": "return a + b"}])
    before = path.read_bytes()
    preview = editor.preview(batch, ["main.py"])
    assert "-    return a - b" in preview["diff"]
    assert path.read_bytes() == before
    path.write_bytes(before + b"\n")
    with pytest.raises(ValueError):
        editor.apply(batch, ["main.py"])


def test_operation_unknown_and_completed_recovery(tmp_path: Path) -> None:
    """未知操作不能重放；已完成结果只读取一次。"""
    path = tmp_path / "ops.sqlite"
    journal = OperationJournal(path)
    calls = []
    def action() -> dict:
        """记录一次受控副作用。"""
        calls.append(1)
        return {"done": True}
    assert journal.run("one", {}, action) == {"done": True}
    assert OperationJournal(path).run("one", {}, action) == {"done": True}
    assert len(calls) == 1
    with sqlite3.connect(path) as db:
        db.execute("UPDATE operations SET status='pending',result=NULL")
    with pytest.raises(RuntimeError, match="禁止自动重放"):
        OperationJournal(path).run("one", {}, action)
    assert journal.run("one", {}, action, lambda: {"done": True}) == {"done": True}
    assert len(calls) == 1
    with pytest.raises(RuntimeError):
        journal.run("one", {"changed": 1}, action)


def test_domain_credentials_and_persistent_redaction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """专用 Key 优先，页面 Key 可传检索，但不能出现在事件中。"""
    monkeypatch.setenv("AICD_WEB_API_KEY", "sentinel-web-credential")
    monkeypatch.setenv("AICD_EMBEDDING_API_KEY", "sentinel-embed-credential")
    assert credential("EMBEDDING") == "sentinel-embed-credential"
    assert credential("RERANK") == "sentinel-web-credential"
    store = Store(tmp_path / "tasks.sqlite")
    store.create("one", {})
    store.update("one", "running", answer="sentinel-web-credential sentinel-embed-credential")
    assert "sentinel" not in str(store.get("one")) + str(store.events("one"))


def test_old_worker_monitor_cannot_block_new_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """旧进程迟到的退出回调不能把审批恢复后的新进程标记 blocked。"""
    import re
    import time
    from threading import Event
    from fastapi.testclient import TestClient
    from web_runtime.app import create_app
    released, second_started, old_finished = Event(), Event(), Event()
    created = []
    class Process:
        """可控制退出时刻的 Worker 替身。"""
        def wait(self) -> int:
            if self is created[0]:
                released.wait(5)
                old_finished.set()
            else:
                second_started.wait(5)
            return 0
        def poll(self) -> int:
            return 0
    def launch(*args: object, **kwargs: object) -> Process:
        process = Process()
        created.append(process)
        return process
    monkeypatch.setattr("web_runtime.app.subprocess.Popen", launch)
    with TestClient(create_app(tmp_path)) as client:
        token = re.search("const token='([^']+)'", client.get("/").text).group(1)
        headers = {"x-local-token": token}
        key = client.post("/api/tasks", headers=headers, json={"repository": "fixture", "request": "解释",
            "mode": "ask", "trusted_copy": True}).json()["task_id"]
        store = Store(tmp_path / "tasks.sqlite")
        store.update(key, "waiting", approval={"id": "one", "kind": "test"})
        assert client.post(f"/api/tasks/{key}/approve", headers=headers,
            json={"approval_id": "one", "approve": True}).status_code == 200
        released.set()
        assert old_finished.wait(3)
        time.sleep(0.1)
        assert store.get(key)["status"] == "running"
        store.update(key, "waiting", approval={"id": "two"})
        second_started.set()


def test_followup_preserves_readonly_conversation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """追问创建新只读任务，带前文且不继承修改授权。"""
    import re
    from fastapi.testclient import TestClient
    from web_runtime.app import create_app, TaskInput
    store = Store(tmp_path / "tasks.sqlite")
    options = TaskInput(repository="fixture", mode="ask", request="解释登录", trusted_copy=True,
                        tests=[], image="").model_dump(exclude={"api_key", "embedding_api_key", "rerank_api_key"})
    store.create("previous", {"options": options})
    store.update("previous", "success", answer="auth.py:10 负责校验")
    def unavailable(*args: object, **kwargs: object) -> None:
        raise OSError("fixture")
    monkeypatch.setattr("web_runtime.app.subprocess.Popen", unavailable)
    with TestClient(create_app(tmp_path)) as client:
        token = re.search("const token='([^']+)'", client.get("/").text).group(1)
        response = client.post("/api/tasks/previous/followup", headers={"x-local-token": token},
            json={"approval_id": "followup", "approve": True, "feedback": "谁调用这个函数？"})
        assert response.status_code == 202
        next_options = store.get(response.json()["task_id"])["options"]
        assert next_options["mode"] == "ask" and next_options["allowed_files"] == []
        assert next_options["request"] == "谁调用这个函数？"
        assert next_options["conversation"][-1]["summary"] == "auth.py:10 负责校验"
