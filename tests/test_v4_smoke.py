"""无需 LLM/MCP/Docker 的 V4 Web 入口冒烟测试。"""

import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


def test_web_readonly_and_local_boundaries(tmp_path):
    from web_runtime.app import create_app

    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/defaults").status_code == 200
        assert client.get("/api/tasks/not-found").status_code == 404
        assert client.post("/api/tasks", json={}).status_code == 403
        assert client.get("/", headers={"host": "evil.example"}).status_code == 403


def test_web_launcher_starts_single_local_worker(monkeypatch):
    import main

    called = []
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=lambda *a, **kw: called.append((a, kw))))
    main.main(["--port", "8312"])
    assert called == [(("web_runtime.app:app",),
                       {"host": "127.0.0.1", "port": 8312, "workers": 1})]


def test_web_launcher_rejects_public_host():
    import main

    with pytest.raises(SystemExit) as exc:
        main.main(["--host", "0.0.0.0"])
    assert exc.value.code == 2
