"""显式开启的真实模型、索引、MCP、Docker 与 Web API 联调。"""

import json
import os
from pathlib import Path
import re
import time

from fastapi.testclient import TestClient
import pytest

from web_runtime.app import create_app
from workspace.manager import git


@pytest.mark.skipif(os.getenv("AICD_RUN_V5_LIVE") != "1", reason="需显式开启真实 API/Docker 联调")
def test_real_model_cross_file_repair_and_followup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """仅给症状，让真实模型跨文件定位并经 Diff 审批修复。"""
    repo = tmp_path / "repository"
    repo.mkdir()
    (repo / "tests").mkdir()
    (repo / "normalization.py").write_text('def normalize_email(value: str) -> str:\n    return value.strip().upper()\n', encoding="utf-8")
    (repo / "directory.py").write_text('from normalization import normalize_email\n\nUSERS = {"alice@example.com": "Alice"}\n\ndef lookup(email: str) -> str | None:\n    return USERS.get(normalize_email(email))\n', encoding="utf-8")
    (repo / "tests/test_directory.py").write_text('from directory import lookup\n\ndef test_lookup():\n    assert lookup(" Alice@Example.com ") == "Alice"\n    assert lookup("alice@example.com") == "Alice"\n    assert lookup("nobody@example.com") is None\n', encoding="utf-8")
    git(repo, "init")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=V5 Test", "-c", "user.email=v5@example.invalid", "commit", "-m", "fixture")
    storage = tmp_path / "runtime"
    # 本轮只通过页面参数提供凭据，检验 Worker/MCP 的真实传递路径。
    key = os.environ.get("DASHSCOPE_API_KEY", "")
    for name in ("DASHSCOPE_API_KEY", "AICD_WEB_API_KEY", "AICD_LLM_API_KEY", "AICD_EMBEDDING_API_KEY", "AICD_RERANK_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    with TestClient(create_app(storage)) as client:
        token = re.search("const token='([^']+)'", client.get("/").text).group(1)
        headers = {"x-local-token": token}
        response = client.post("/api/tasks", headers=headers, json={"repository": str(repo),
            "request": "修复按电子邮件查询用户时大小写不一致导致找不到用户的问题，保留空格清理，确保原有测试通过。先运行测试复现问题，再修复并重新测试。",
            "mode": "repair", "trusted_copy": True, "image": os.getenv("AICD_V5_TEST_IMAGE", "aicd-pytest:local"),
            "allowed_files": [], "tests": ["tests"], "api_key": key, "budget": {"seconds": 600}})
        assert response.status_code == 202
        task_id = response.json()["task_id"]
        patches = []
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            view = client.get("/api/tasks/" + task_id).json()
            if view["status"] == "waiting":
                approval = view["approval"]
                if approval["kind"] == "patch":
                    assert approval["diff"] and approval["patch_hash"]
                    patches.append(approval["patch_hash"])
                assert client.post(f"/api/tasks/{task_id}/approve", headers=headers,
                    json={"approval_id": approval["id"], "approve": True}).status_code == 200
            elif view["status"] in {"success", "failed", "blocked", "cancelled"}:
                break
            time.sleep(0.25)
        assert view["status"] == "success", {"status": view["status"], "error": view.get("error"),
            "result": view.get("result"), "diagnostics": view.get("diagnostics")}
        assert patches
        assert view["result"]["test_result"]["passed"] >= 1
        assert not git(repo, "status", "--porcelain=v1", "--untracked-files=all")
        preview = client.get(f"/api/tasks/{task_id}/apply-preview")
        assert preview.status_code == 200, preview.text
        assert client.post(f"/api/tasks/{task_id}/apply", headers=headers, json={"confirm": True}).status_code == 200
        changed = set(git(repo, "diff", "--name-only").splitlines())
        assert changed and changed <= {"normalization.py", "directory.py"}
        if key:
            assert key not in json.dumps(view)
            for path in storage.rglob("*"):
                if path.is_file():
                    assert key.encode() not in path.read_bytes(), f"凭据泄露到 {path.name}"
        events = [json.loads(line) for line in (storage / task_id / "tool_events.jsonl").read_text(encoding="utf-8").splitlines()]
        assert {"normalization.py", "directory.py"} <= {e["arguments"].get("path") for e in events}
        # 保存真实证据到显式指定目录，默认仍留在 pytest 临时产物中。
        output = os.getenv("AICD_V5_REPORT_DIR")
        if output:
            target = Path(output)
            target.mkdir(parents=True, exist_ok=True)
            (target / "live_result.json").write_text(json.dumps({"task_id": task_id,
                "report": view["result"]["final_report"], "patches": patches, "tool_events": events,
                "runtime_directory": str(storage)}, ensure_ascii=False, indent=2), encoding="utf-8")
