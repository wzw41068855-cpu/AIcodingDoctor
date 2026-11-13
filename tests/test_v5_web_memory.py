"""经验管理、用户补充与 Web 持久事件回归。"""

from pathlib import Path
import re

from fastapi.testclient import TestClient

from web_runtime.app import create_app
from web_runtime.store import Store


def test_explicit_experience_api_and_project_isolation(tmp_path: Path) -> None:
    """保存/编辑/删除必须经本地令牌与明确确认。"""
    with TestClient(create_app(tmp_path)) as client:
        token = re.search("const token='([^']+)'", client.get("/").text).group(1)
        headers = {"x-local-token": token}
        data = {"repository": "repo1", "content": "SQL 使用参数绑定"}
        assert client.post("/api/experiences", json=data).status_code == 403
        assert client.post("/api/experiences", headers=headers, json=data).status_code == 422
        data["explicit_confirm"] = True
        identity = client.post("/api/experiences", headers=headers, json=data).json()["id"]
        assert client.get("/api/experiences", params={"repository": "repo2"}).json() == []
        data.update(experience_id=identity, content="SQL 始终使用参数化查询")
        assert client.post("/api/experiences", headers=headers, json=data).status_code == 200
        assert "始终" in client.get("/api/experiences", params={"repository": "repo1"}).json()[0]["content"]
        assert client.delete(f"/api/experiences/{identity}", headers=headers, params={"repository": "repo2"}).status_code == 404
        assert client.delete(f"/api/experiences/{identity}", headers=headers, params={"repository": "repo1"}).status_code == 200
        assert client.get("/api/experiences", params={"repository": "repo1"}).json() == []
        assert client.get("/workspace.js").status_code == 200


def test_feedback_persists_and_does_not_override_cancellation(tmp_path: Path) -> None:
    """新反馈形成有序事件，重建 Store 后仍可读，不能重新启动任务。"""
    store = Store(tmp_path / "tasks.sqlite")
    store.create("task", {})
    with TestClient(create_app(tmp_path)) as client:
        # 启动恢复会阻塞 queued，显式建立测试运行态而不启动进程。
        store.create("active", {})
        store.claim("active", "queued")
        token = re.search("const token='([^']+)'", client.get("/").text).group(1)
        headers = {"x-local-token": token}
        assert client.post("/api/tasks/active/feedback", headers=headers, json={"feedback": "保留原接口"}).status_code == 200
        events = store.events("active")
        assert events and store.events("active", events[-1][0]) == []
        restored = Store(tmp_path / "tasks.sqlite")
        assert restored.get("active")["user_feedback"][0]["text"] == "保留原接口"
        store.update("active", "cancelling")
        assert client.post("/api/tasks/active/feedback", headers=headers, json={"feedback": "修改"}).status_code == 409
        assert store.get("active")["status"] == "cancelling"
