"""V4 离线验证；所有状态和产物只写入临时目录。"""

from types import SimpleNamespace
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from graph.state import TaskPlan
from web_runtime.store import Store
from web_runtime.workflow import build_graph


def test_store_claim_and_replay(tmp_path):
    """审批只能领取一次；重建存储后仍能读到有序事件。"""
    store = Store(tmp_path / "tasks.sqlite")
    store.create("one", {})
    store.update("one", "waiting", approval={"id": "a"})
    assert store.claim("one", "waiting")
    assert not store.claim("one", "waiting")
    restored = Store(tmp_path / "tasks.sqlite")
    assert restored.get("one")["status"] == "running"
    assert len(restored.events("one")) == 1
    assert restored.events("one", restored.events("one")[0][0]) == []


def test_missing_task(tmp_path):
    """未知任务不会生成伪造视图。"""
    import pytest
    with pytest.raises(KeyError):
        Store(tmp_path / "tasks.sqlite").get("missing")


def test_store_serializes_nested_node_models(tmp_path):
    """真实 Coder 输出中的嵌套 ChangeRecord 可保存且事件能重放。"""
    from graph.state import ChangeRecord, TestResult
    store = Store(tmp_path / "tasks.sqlite")
    store.create("task", {})
    change = ChangeRecord(path="a.py", before_hash="a" * 64, after_hash="b" * 64)
    store.update("task", "waiting", result={"changes": [change]},
        node_results={"Coder": {"changes": [change], "iteration": 1},
                      "Tester": {"test_result": TestResult(outcome="failed", failed=1)}})
    view = store.get("task")
    assert view["result"]["changes"][0]["path"] == "a.py"
    assert view["node_results"]["Tester"]["test_result"]["failed"] == 1
    assert '"Coder"' in store.events("task")[0][1]


def test_apply_api_confirmation_and_duplicate_request(tmp_path, monkeypatch):
    """应用需要本地令牌和明确确认；重复点击只执行一次，未知结果禁止重放。"""
    import re
    from fastapi.testclient import TestClient
    from web_runtime.app import create_app
    application = create_app(tmp_path)
    store = Store(tmp_path / "tasks.sqlite")
    store.create("ready", {"options": {"mode": "repair"}})
    store.update("ready", "success")
    calls = []
    monkeypatch.setattr("web_runtime.apply_changes.prepare_application", lambda *a: (
        None, None, {"repository": "fixture", "files": ["a.py"]}))
    def apply(*args):
        """模拟已校验写回，统计应用次数。"""
        calls.append(True)
        return [{"path": "a.py"}]
    monkeypatch.setattr("web_runtime.apply_changes.apply_application", apply)
    with TestClient(application) as client:
        headers = {"x-local-token": re.search("const token='([^']+)'", client.get("/").text).group(1)}
        assert client.get("/api/tasks/ready/apply-preview").json()["files"] == ["a.py"]
        assert client.post("/api/tasks/ready/apply", json={"confirm": True}).status_code == 403
        assert client.post("/api/tasks/ready/apply", headers=headers, json={"confirm": False}).status_code == 422
        for _ in range(2):
            assert client.post("/api/tasks/ready/apply", headers=headers, json={"confirm": True}).status_code == 200
        assert len(calls) == 1
        assert store.get("ready")["application_status"] == "applied"
        store.update("ready", "success", application_status="uncertain")
        assert client.post("/api/tasks/ready/apply", headers=headers, json={"confirm": True}).status_code == 409


def test_plan_reject_replan_and_edit_gate(tmp_path):
    """拒绝时不修改；意见触发重规划；批准计划仍需独立修改授权。"""
    calls = []
    plan = TaskPlan(summary="修复", target_files=["a.py"], steps=["修复"],
                    acceptance_criteria=["测试通过"], test_strategy="pytest")
    def planner(state):
        """模拟 Planner，记录规划次数。"""
        calls.append("plan")
        return {"plan": plan, "status": "running"}
    def coder(state):
        """模拟有副作用节点，记录是否被错误调用。"""
        calls.append("edit")
        return {"status": "blocked"}
    nodes = SimpleNamespace(planner=planner, coder=coder, tester=lambda s: {}, reviewer=lambda s: {},
        finalizer=lambda s: {}, runner=SimpleNamespace(tests=["tests"]), guarded=lambda n, f: f)
    import sqlite3
    from langgraph.checkpoint.sqlite import SqliteSaver
    connection = sqlite3.connect(tmp_path / "graph.sqlite", check_same_thread=False)
    saver = SqliteSaver(connection)
    graph = build_graph(nodes, saver)
    cfg = {"configurable": {"thread_id": "test"}}
    graph.invoke({"status": "running", "user_request": "修复", "iteration": 0}, cfg)
    graph.invoke(Command(resume={"approve": False, "feedback": "调整方案"}), cfg)
    assert calls == ["plan", "plan"]
    connection.close()
    connection = sqlite3.connect(tmp_path / "graph.sqlite", check_same_thread=False)
    graph = build_graph(nodes, SqliteSaver(connection))
    graph.invoke(Command(resume={"approve": True}), cfg)
    assert calls == ["plan", "plan"]
    result = graph.invoke(Command(resume={"approve": False}), cfg)
    assert result["status"] == "blocked"
    assert "edit" not in calls
    connection.close()


def test_web_local_boundary(tmp_path):
    """未知任务返回 404；无令牌的跨站写入和非法 Host 被拒绝。"""
    from fastapi.testclient import TestClient
    from web_runtime.app import create_app
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/tasks/nope").status_code == 404
        assert client.post("/api/tasks", json={}).status_code == 403
        assert client.get("/", headers={"host": "evil.example"}).status_code == 403


def test_defaults_and_repository_picker(tmp_path, monkeypatch):
    """默认配置不暴露凭据；文件夹选择支持成功、取消和无效目录。"""
    import re
    import config
    from fastapi.testclient import TestClient
    from web_runtime.app import create_app
    monkeypatch.setenv("DASHSCOPE_API_KEY", "never-return-this-key")
    monkeypatch.setattr("web_runtime.options.pick_repository", lambda: None)
    with TestClient(create_app(tmp_path)) as client:
        html = client.get("/").text
        headers = {"x-local-token": re.search("const token='([^']+)'", html).group(1)}
        response = client.get("/api/defaults")
        assert response.json()["model"] == config.LLM_MODEL
        assert response.json()["api_key_configured"] is True
        assert "never-return-this-key" not in response.text + html
        assert 'type="password"' in html and 'id="pick"' in html
        assert client.post("/api/repositories/select", headers=headers).json() == {"cancelled": True}
        monkeypatch.setattr("web_runtime.options.pick_repository", lambda: str(tmp_path))
        assert client.post("/api/repositories/select", headers=headers).status_code == 422
        monkeypatch.setattr("web_runtime.options.repository_options", lambda p: {
            "repository": p, "allowed_files": ["main.py"], "tests": ["tests"]})
        assert client.post("/api/repositories/select", headers=headers).json()["allowed_files"] == ["main.py"]
        assert client.post("/api/repositories/select").status_code == 403


def test_repository_dropdown_candidates(tmp_path, monkeypatch):
    """候选清单排除测试、配置、凭据、越界路径及不存在文件。"""
    from web_runtime.options import repository_options
    (tmp_path / "tests").mkdir()
    for name in ["main.py", "config.py", "test_unit.py", "tests/test_main.py"]:
        (tmp_path / name).write_text("", encoding="utf-8")
    def fake_git(root, *args):
        """返回固定 Git 文件名，不读取源码内容。"""
        return str(tmp_path) if args[0] == "rev-parse" else "main.py\0config.py\0test_unit.py\0tests/test_main.py\0.env\0../escape.py\0missing.py\0"
    monkeypatch.setattr("web_runtime.options.git", fake_git)
    result = repository_options(tmp_path)
    assert result["allowed_files"] == ["main.py"]
    assert result["tests"] == ["test_unit.py", "tests", "tests/test_main.py"]


def test_local_image_options(monkeypatch):
    """镜像仅查询本地；Docker 失败或超大输出时返回可显示错误。"""
    from web_runtime.options import local_images
    monkeypatch.setattr("sandbox.docker_runner.capture", lambda *a, **k: (
        0, b"fixture:local\tabc\n<none>:<none>\tdef\nfixture:local\tabc\n", False))
    assert local_images()["images"] == ["def", "fixture:local"]
    monkeypatch.setattr("sandbox.docker_runner.capture", lambda *a, **k: (1, b"", False))
    assert local_images()["error"]


def test_native_picker_process(monkeypatch):
    """目录选择子进程支持 Unicode 路径、取消与失败，环境不传模型密钥。"""
    import json
    import pytest
    from web_runtime.options import pick_repository
    monkeypatch.setenv("DASHSCOPE_API_KEY", "never-inherit")
    def selected(*args, **kwargs):
        """模拟系统对话框，并检查无 Shell、无凭据继承。"""
        assert kwargs["shell"] is False
        assert "DASHSCOPE_API_KEY" not in kwargs["env"]
        return SimpleNamespace(returncode=0, stdout=json.dumps("C:/项目/repository").encode())
    monkeypatch.setattr("web_runtime.options.subprocess.run", selected)
    assert pick_repository() == "C:/项目/repository"
    monkeypatch.setattr("web_runtime.options.subprocess.run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=b'""'))
    assert pick_repository() is None
    monkeypatch.setattr("web_runtime.options.subprocess.run", lambda *a, **k: SimpleNamespace(returncode=1))
    with pytest.raises(RuntimeError):
        pick_repository()


def test_api_immediate_id_and_duplicate_approval(tmp_path, monkeypatch):
    """进程启动失败仍保留 task_id 和阻塞原因；同一审批只能领取一次。"""
    import re
    from fastapi.testclient import TestClient
    from web_runtime.app import create_app

    def fail_start(*args, **kwargs):
        """模拟系统拒绝创建进程，不启动真实模型或容器。"""
        raise OSError("fixture")

    monkeypatch.setattr("web_runtime.app.subprocess.Popen", fail_start)
    with TestClient(create_app(tmp_path)) as client:
        token = re.search("const token='([^']+)'", client.get("/").text).group(1)
        headers = {"x-local-token": token}
        response = client.post("/api/tasks", headers=headers, json={"repository": "fixture",
            "request": "修复", "trusted_copy": True, "allowed_files": ["a.py"], "api_key": "do-not-persist"})
        assert response.status_code == 202
        task_id = response.json()["task_id"]
        assert client.get("/api/tasks/" + task_id).json()["status"] == "blocked"
        store = Store(tmp_path / "tasks.sqlite")
        assert "do-not-persist" not in str(store.get(task_id))
        store.update(task_id, "waiting", approval={"id": "one", "kind": "edit"})
        decision = {"approval_id": "one", "approve": True}
        assert client.post(f"/api/tasks/{task_id}/approve", headers=headers, json=decision).status_code == 200
        assert client.post(f"/api/tasks/{task_id}/approve", headers=headers, json=decision).status_code == 409
