"""分离的聊天入口与连续问答持久化契约。"""

from pathlib import Path
import re

from fastapi.testclient import TestClient

from web_runtime.app import create_app, TaskInput
from web_runtime.store import Store


def test_chat_and_repair_entry_assets(tmp_path: Path) -> None:
    """两个入口提供本地资源，写接口继续要求本地令牌。"""
    with TestClient(create_app(tmp_path)) as client:
        page = client.get("/").text
        for identity in ("nav-ask", "nav-repair", "chat-form", "messages", "repair-panel", "repair-detail"):
            assert f'id="{identity}"' in page
        for asset, media in (("workbench.js", "javascript"), ("chat.js", "javascript"), ("workbench.css", "text/css")):
            response = client.get("/" + asset)
            assert response.status_code == 200 and media in response.headers["content-type"]
        assert client.get("/unknown.js").status_code == 404
        assert client.post("/api/tasks", json={}).status_code == 403


def test_two_followups_retain_chat_after_restart(tmp_path: Path, monkeypatch) -> None:
    """连续两次追问持久保存双向消息，始终继承只读授权。"""
    def offline(*args: object, **kwargs: object) -> None:
        """阻止启动真实模型，仅检验 Web 会话契约。"""
        raise OSError("offline fixture")

    monkeypatch.setattr("web_runtime.app.subprocess.Popen", offline)
    store = Store(tmp_path / "tasks.sqlite")
    options = TaskInput(repository="fixture", request="解释登录", mode="ask", trusted_copy=True,
                        allowed_files=[], tests=[], image="").model_dump(exclude={"api_key", "embedding_api_key", "rerank_api_key"})
    store.create("first", {"options": options})
    store.update("first", "success", answer="auth.py:1 登录入口")
    with TestClient(create_app(tmp_path)) as client:
        token = re.search("const token='([^']+)'", client.get("/").text).group(1)
        headers = {"x-local-token": token}
        second = client.post("/api/tasks/first/followup", headers=headers,
                             json={"approval_id": "followup", "approve": True, "feedback": "谁调用它？"}).json()["task_id"]
        # 模拟离线 Worker 完成，保留真实接口产生的上下文。
        store.update(second, "waiting", approval={"id": "fixture"})
        store.claim(second, "waiting")
        store.update(second, "success", answer="api.py:8 调用登录")
        response = client.post(f"/api/tasks/{second}/followup", headers=headers,
                               json={"approval_id": "followup", "approve": True, "feedback": "异常怎样处理？"})
        assert response.status_code == 202
        last = response.json()["task_id"]
        assert client.post(f"/api/tasks/{last}/followup", headers=headers,
                           json={"approval_id": "followup", "approve": True, "feedback": "重复提交"}).status_code == 409
    with TestClient(create_app(tmp_path)) as client:
        restored = client.get(f"/api/tasks/{last}").json()["options"]
        assert restored["mode"] == "ask" and restored["tests"] == restored["allowed_files"] == []
        assert [m["summary"] for m in restored["conversation"]] == ["解释登录", "auth.py:1 登录入口", "谁调用它？", "api.py:8 调用登录"]
        assert restored["request"] == "异常怎样处理？"
