"""V5 真实 MCP 认证、审批绑定、恢复和只读初始化测试。"""

import json
from pathlib import Path
from types import SimpleNamespace

from anyio.from_thread import start_blocking_portal
from mcp import Client
import pytest

from graph.agent_tools import AgentTools, ToolAction
from graph.demo import demo_service
from graph.editor import sha256
from graph.operations import digest
from mcp_client.client import ToolRPC, connect
from mcp_servers.context import ToolContext
from mcp_servers.server import create_server
from web_runtime.task_setup import initialize


def test_subgraph_patch_rejection_and_sqlite_resume(task, monkeypatch: pytest.MonkeyPatch) -> None:
    """真实子图中断经 SQLite 重建恢复，拒绝候选补丁不写文件。"""
    import sqlite3
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.types import Command
    from graph.autonomous import AutonomousAgents, Schedule
    from graph.budget import Budget
    from graph.state import EditBatch
    _, manager, config = task
    config = config.model_copy(update={"enforce_roles": True})
    monkeypatch.setenv("AICD_ROLE_SECRET", "host-only-secret")
    config_path = manager.artifacts / "task.json"
    config_path.write_text(config.model_dump_json(), encoding="utf-8")
    before = (manager.root / "calculator.py").read_bytes()

    class Model:
        """先生成候选补丁，拒绝后返回明确停止结果。"""
        def __init__(self) -> None:
            self.phase, self.step = 0, 0

        def with_structured_output(self, schema: type, **kwargs: object) -> SimpleNamespace:
            """读取真实 snapshot 来构造版本正确的补丁。"""
            def invoke(messages: list, config: dict | None = None) -> dict:
                if schema is Schedule:
                    self.phase += 1
                    return {"next_role": "Coder" if self.phase == 1 else "blocked", "reason": "验证拒绝补丁"}
                payload = json.loads(messages[-1][1])
                self.step += 1
                if self.step == 1:
                    return {"tool": "snapshot", "arguments": {"path": "calculator.py"}}
                if self.step == 2:
                    snap = payload["history"][-1]["result"]
                    return {"tool": "apply_patch", "arguments": {"batch": {"edits": [{
                        "path": "calculator.py", "expected_hash": snap["expected_hash"],
                        "old_text": "return a - b", "new_text": "return a + b"}]}}}
                return {"summary": "用户拒绝补丁，交还调度", "verdict": "retry"}
            return SimpleNamespace(invoke=invoke)

    model = Model()
    budget = Budget(manager.artifacts / "budget.sqlite")
    cfg = {"configurable": {"thread_id": "reject"}}
    with connect(config_path, 30) as rpc:
        agents = AutonomousAgents(model, AgentTools(rpc, manager.artifacts, config.allowed_files, "repair"), budget)
        with sqlite3.connect(manager.artifacts / "graph.sqlite", check_same_thread=False) as db:
            graph = agents.build(SqliteSaver(db))
            graph.invoke({"mode": "repair", "user_request": "修复加法", "status": "running", "max_iterations": 3}, cfg)
            assert graph.get_state(cfg).tasks[0].interrupts[0].value["kind"] == "plan"
            graph.invoke(Command(resume={"approve": True}), cfg)
            pending = graph.get_state(cfg).tasks[0].interrupts
            assert pending and pending[0].value["kind"] == "patch"
            assert "return a + b" in pending[0].value["diff"]
        assert (manager.root / "calculator.py").read_bytes() == before
        with sqlite3.connect(manager.artifacts / "graph.sqlite", check_same_thread=False) as db:
            restored = agents.build(SqliteSaver(db))
            result = restored.invoke(Command(resume={"approve": False, "feedback": "不修改"}), cfg)
        assert result["status"] == "blocked"
        assert (manager.root / "calculator.py").read_bytes() == before
        assert budget.snapshot()["tool_calls"] == 2


def test_authenticated_stdio_preview_apply_and_role_rejection(task, monkeypatch: pytest.MonkeyPatch) -> None:
    """真实 stdio 中，角色冒充拒绝且已批准补丁只应用一次。"""
    _, manager, config = task
    config = config.model_copy(update={"enforce_roles": True})
    monkeypatch.setenv("AICD_ROLE_SECRET", "host-only-secret")
    path = manager.artifacts / "task.json"
    path.write_text(config.model_dump_json(), encoding="utf-8")
    with connect(path, 30) as rpc:
        rpc.node = "Explorer"
        assert rpc.call("list_files")["files"]
        with pytest.raises((ValueError, RuntimeError)):
            rpc.call("snapshot", path="calculator.py")
        rpc.node = "Coder"
        snap = rpc.call("snapshot", path="calculator.py")
        batch = {"edits": [{"path": "calculator.py", "expected_hash": snap["expected_hash"],
            "old_text": "return a - b", "new_text": "return a + b"}]}
        preview = rpc.call("preview_patch", batch=batch, allowed=["calculator.py"])
        assert "return a + b" in preview["diff"]
        assert "return a - b" in (manager.root / "calculator.py").read_text(encoding="utf-8")
        (manager.artifacts / ("approved_" + preview["patch_hash"] + ".json")).write_text(json.dumps(preview), encoding="utf-8")
        result = rpc.call("apply_patch", batch=batch, allowed=["calculator.py"], _operation_id="patch:1")
        assert rpc.call("apply_patch", batch=batch, allowed=["calculator.py"], _operation_id="patch:1") == result
        rpc.node = "Reviewer"
        assert "return a + b" in rpc.call("git_diff")
        with pytest.raises((ValueError, RuntimeError)):
            rpc.call("run_pytest", _operation_id="test:bad")
    # 模拟写入已完成、Graph checkpoint 尚未登记时进程退出。
    import sqlite3
    with sqlite3.connect(manager.artifacts / "operations.sqlite") as db:
        db.execute("UPDATE operations SET status='pending',result=NULL WHERE id='patch:1'")
    with connect(path, 30) as rpc:
        rpc.node = "Coder"
        assert rpc.call("apply_patch", batch=batch, allowed=["calculator.py"], _operation_id="patch:1") == result
        source = manager.root / "calculator.py"
        source.write_bytes(source.read_bytes() + b"\n")
        with pytest.raises(RuntimeError):
            rpc.call("apply_patch", batch=batch, allowed=["calculator.py"], _operation_id="patch:1")


def test_readonly_setup_never_constructs_repair_resources(task, monkeypatch: pytest.MonkeyPatch) -> None:
    """没有镜像与修改白名单也能创建只读 MCP 上下文。"""
    repo, manager, config = task
    artifacts = manager.artifacts / config.task_id
    artifacts.mkdir()
    def forbidden(*args: object, **kwargs: object) -> None:
        """任何修复资源构造都立即失败。"""
        raise AssertionError("只读入口构造了修复资源")
    monkeypatch.setattr("web_runtime.task_setup.WorktreeManager", forbidden)
    monkeypatch.setattr("sandbox.docker_runner.DockerRunner", forbidden)
    readonly = initialize(artifacts, {"repository": str(repo), "mode": "ask", "trusted_copy": True, "timeout": 30})
    service = demo_service(repo, artifacts)
    ctx = ToolContext(readonly, service=service)
    assert ctx.runner is None and ctx.editor is None
    assert ctx.service.get_code_context("calculator.py").content


def test_regression_tests_only_created_in_authorized_directory(task, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tester 不能覆盖已有测试或借新建接口写业务代码。"""
    _, manager, config = task
    config = config.model_copy(update={"enforce_roles": True, "allow_regression_tests": True})
    monkeypatch.setenv("AICD_ROLE_SECRET", "host-only-secret")
    path = manager.artifacts / "task.json"
    path.write_text(config.model_dump_json(), encoding="utf-8")
    with connect(path, 30) as rpc:
        rpc.node = "Tester"
        path = "tests/v5_generated/test_regression.py"
        assert rpc.call("add_regression_test", path=path, content="def test_new():\n    assert True\n")["created"]
        for invalid in [path, "tests/test_calculator.py", "calculator.py", "../test_escape.py"]:
            with pytest.raises((ValueError, RuntimeError)):
                rpc.call("add_regression_test", path=invalid, content="assert False\n")
    from graph.editor import Workspace
    from sandbox.docker_runner import DockerRunner
    runner = DockerRunner(Workspace(manager.root, trusted_copy=True), config.tests, config.image,
                          task_id=config.task_id, artifacts=manager.artifacts)
    destination = manager.artifacts / "snapshot"
    destination.mkdir()
    runner.snapshot(destination)
    assert (destination / "tests/v5_generated/test_regression.py").exists()
    (manager.root / "tests/v5_generated/test_regression.py").write_text("assert False\n", encoding="utf-8")
    second = manager.artifacts / "tampered"
    second.mkdir()
    with pytest.raises(ValueError, match="发生变化"):
        runner.snapshot(second)


@pytest.mark.parametrize("change_before_report", [False, True])
def test_readonly_worker_returns_answer_without_docker(task, monkeypatch: pytest.MonkeyPatch,
                                                     change_before_report: bool) -> None:
    """只读链路无需 Docker，报告发布前源码改变则引用失效并阻塞。"""
    from uuid import uuid4
    from web_runtime.app import TaskInput
    from web_runtime.store import Store
    from web_runtime.worker import run
    import web_runtime.task_setup
    repo, manager, _ = task
    key = uuid4().hex
    directory = manager.artifacts / key
    directory.mkdir()
    original = web_runtime.task_setup.initialize
    def setup(path: Path, options: dict):
        """保留真实只读初始化，仅使用已有的离线索引后端。"""
        config = original(path, options).model_copy(update={"mock_index": True})
        (path / "task.json").write_text(config.model_dump_json(), encoding="utf-8")
        return config
    script = iter([{"next_role": "Explorer", "reason": "读取代码"},
        {"tool": "read_file", "arguments": {"path": "calculator.py"}},
        {"summary": "calculator.py:3 add 当前使用减法，会导致加法测试失败。"},
        {"next_role": "finish", "reason": "已经解释实现"}])
    def invoke(*args, **kwargs):
        """在 Explorer 已验证证据后，模拟用户修改原始源码。"""
        action = next(script)
        if change_before_report and action.get("next_role") == "finish":
            source = repo / "calculator.py"
            source.write_bytes(source.read_bytes() + b"\n# external change\n")
        return action
    model = SimpleNamespace(with_structured_output=lambda *a, **k: SimpleNamespace(invoke=invoke))
    monkeypatch.setattr("web_runtime.task_setup.initialize", setup)
    monkeypatch.setattr("langchain_openai.ChatOpenAI", lambda **kwargs: model)
    monkeypatch.setenv("AICD_ROLE_SECRET", "")
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("不应创建 DockerRunner")
    monkeypatch.setattr("sandbox.docker_runner.DockerRunner", forbidden)
    options = TaskInput(repository=str(repo), request="解释加法实现", mode="ask", trusted_copy=True,
                        tests=[], image="").model_dump(exclude={"api_key", "embedding_api_key", "rerank_api_key"})
    store = Store(directory.parent / "tasks.sqlite")
    store.create(key, {"options": options})
    store.claim(key, "queued")
    run(directory)
    view = store.get(key)
    if change_before_report:
        assert view["status"] == view["result"]["status"] == "blocked", view
        assert view["result"]["final_report"]["status"] == "blocked"
    else:
        assert view["status"] == "success", view
        assert "calculator.py:3" in view["answer"]
    assert not (directory / "worktree").exists()
