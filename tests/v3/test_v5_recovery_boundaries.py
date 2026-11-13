"""四类副作用边界的恢复，以及 SSE 断线续传契约。"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from graph.budget import Budget
from graph.editor import Editor, Workspace
from graph.operations import OperationJournal
from graph.state import EditBatch
from web_runtime.app import create_app
from web_runtime.store import Store


def test_waiting_diff_and_budget_survive_service_restart(task) -> None:
    """服务重建不会丢失待审批 Diff、源哈希或已用预算。"""
    _, manager, config = task
    root = manager.artifacts.parent
    editor = Editor(Workspace(manager.root, trusted_copy=True))
    snap = editor.snapshot("calculator.py")
    batch = EditBatch(edits=[{"path": "calculator.py", "expected_hash": snap["expected_hash"],
        "old_text": "return a - b", "new_text": "return a + b"}])
    preview = editor.preview(batch, ["calculator.py"])
    budget = Budget(manager.artifacts / "budget.sqlite")
    budget.reserve("model_calls", "one")
    store = Store(root / "tasks.sqlite")
    store.create("task", {"options": {"mode": "repair"}})
    store.update("task", "waiting", approval={"id": "pending", "kind": "patch", **preview}, budget=budget.snapshot())
    before = (manager.root / "calculator.py").read_bytes()
    for _ in range(2):
        with TestClient(create_app(root)) as client:
            view = client.get("/api/tasks/task").json()
            assert view["status"] == "waiting" and view["approval"]["patch_hash"] == preview["patch_hash"]
            assert view["budget"]["model_calls"] == 1
    assert (manager.root / "calculator.py").read_bytes() == before


def test_pending_test_and_uncertain_final_apply_are_not_replayed(tmp_path: Path, monkeypatch) -> None:
    """测试中断和最终写回附近重启，均不自动重放未知操作。"""
    from graph.operations import digest
    import sqlite3
    journal = OperationJournal(tmp_path / "operations.sqlite")
    with sqlite3.connect(journal.path) as db:
        db.execute("INSERT INTO operations VALUES (?,?,?,NULL)", ("test", digest({"tests": ["tests"]}), "pending"))
    calls = []
    try:
        journal.run("test", {"tests": ["tests"]}, lambda: calls.append(True))
    except RuntimeError:
        pass
    assert not calls
    store = Store(tmp_path / "tasks.sqlite")
    store.create("apply", {"options": {"mode": "repair"}})
    store.update("apply", "success", application_status="applying")
    def forbidden(*args, **kwargs):
        raise AssertionError("禁止重放写回")
    monkeypatch.setattr("web_runtime.apply_changes.apply_application", forbidden)
    import re
    for _ in range(2):
        with TestClient(create_app(tmp_path)) as client:
            token = re.search("const token='([^']+)'", client.get("/").text).group(1)
            assert client.post("/api/tasks/apply/apply", headers={"x-local-token": token}, json={"confirm": True}).status_code == 409


def test_sse_reconnect_starts_after_last_event_id(tmp_path: Path) -> None:
    """实际事件路由只重放 Last-Event-ID 之后的事件。"""
    app = create_app(tmp_path)
    store = Store(tmp_path / "tasks.sqlite")
    store.create("task", {})
    store.update("task", "running", progress="first")
    cursor = store.events("task")[-1][0]
    store.update("task", "running", progress="second")
    endpoint = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/api/tasks/{task_id}/events")
    async def collect() -> str:
        checks = []
        async def disconnected() -> bool:
            checks.append(True)
            return len(checks) > 1
        request = SimpleNamespace(headers={"last-event-id": str(cursor)}, is_disconnected=disconnected)
        response = await endpoint("task", request, 0)
        return "".join([chunk async for chunk in response.body_iterator])
    received = asyncio.run(collect())
    assert "second" in received and "first" not in received


def test_old_waiting_worker_never_cleans_new_owner_containers(task, monkeypatch) -> None:
    """审批被立即领取后，旧 Worker 不得清理新 Worker 的同任务容器。"""
    from web_runtime.app import TaskInput
    from web_runtime.worker import run
    _, manager, config = task
    config = config.model_copy(update={"enforce_roles": True})
    (manager.artifacts / "task.json").write_text(config.model_dump_json(), encoding="utf-8")
    store = Store(manager.artifacts.parent / "tasks.sqlite")
    options = TaskInput(repository="fixture", request="修复加法", trusted_copy=True,
                        allowed_files=["calculator.py"]).model_dump()
    store.create("task", {"options": options})
    store.claim("task", "queued")
    monkeypatch.setattr("web_runtime.task_setup.initialize", lambda *args: config)
    model = SimpleNamespace(with_structured_output=lambda *a, **k: SimpleNamespace(
        invoke=lambda *a, **k: {"next_role": "Coder", "reason": "审批后修改"}))
    monkeypatch.setattr("langchain_openai.ChatOpenAI", lambda **kwargs: model)
    monkeypatch.setenv("AICD_ROLE_SECRET", "")
    original = Store.update
    def immediate_claim(self, identity, status, **fields):
        result = original(self, identity, status, **fields)
        if status == "waiting":
            assert self.claim(identity, "waiting")
        return result
    monkeypatch.setattr(Store, "update", immediate_claim)
    calls = []
    def forbidden(self):
        calls.append(True)
        raise RuntimeError("旧 Worker 清理了新 Worker")
    monkeypatch.setattr("sandbox.docker_runner.DockerRunner.cleanup_remaining", forbidden)
    run(manager.artifacts)
    assert not calls
    assert store.get("task")["status"] == "running"
