"""真实 MCP 协议复用原修复图；离线测试替换模型、索引及容器结果。"""

from contextlib import ExitStack
import json
from pathlib import Path
from types import SimpleNamespace
from threading import Event

import anyio
from anyio.from_thread import start_blocking_portal
from mcp import Client
import pytest

from audit.writer import AuditError
from graph.demo import DemoModel
from graph.state import EditBatch, TestResult
from graph.workflow import RepairWorkflow
from mcp_client.client import connect, EditorProxy, RunnerProxy, SearchProxy, ToolRPC
from mcp_servers.context import ToolContext
from mcp_servers.server import create_server


def context(task):
    """注入确定性容器结果替身，绝不执行宿主 pytest 或项目源码。"""
    _, manager, config = task

    def run():
        """根据可信 fixture 的已知修复文本生成测试结果，不动态执行文件。"""
        correct = "return a + b\n" in (manager.root / "calculator.py").read_text(encoding="utf-8")
        return TestResult(outcome="passed" if correct else "failed", returncode=0 if correct else 1,
                          passed=int(correct), failed=int(not correct), output="fixture result")

    return ToolContext(config, runner=SimpleNamespace(run=run))


@pytest.mark.parametrize("mode,rounds,status", [("success", 1, "success"), ("retry", 2, "success"),
                                               ("limit", 3, "failed")])
def test_graph_through_real_mcp(task, mode, rounds, status):
    """原 Graph 经官方 MCP 内存传输完成成功、失败反馈和达到上限三个分支。"""
    repo, manager, config = task
    ctx = context(task)
    with ExitStack() as stack:
        portal = stack.enter_context(start_blocking_portal())
        client = stack.enter_context(portal.wrap_async_context_manager(Client(create_server(ctx))))
        rpc = ToolRPC(portal, client, 20)
        ws = ctx.workspace
        flow = RepairWorkflow(ws, DemoModel(mode), tests=config.tests,
            service=SearchProxy(ws.root, rpc), editor=EditorProxy(ws, rpc),
            runner=RunnerProxy(ws, config.tests, rpc))
        state = flow.run("修复 calculator.py 的 add 加法")
    assert state["status"] == status, state
    assert state["iteration"] == rounds
    events = [json.loads(s) for s in ctx.audit.path.read_text(encoding="utf-8").splitlines()]
    tools = {e["tool"] for e in events}
    assert {"search_code", "read_file", "snapshot", "apply_patch", "sync_changed_files", "run_pytest", "git_diff"} <= tools
    assert "a - b" in (repo / "calculator.py").read_text(encoding="utf-8")
    assert (manager.root / "calculator.py").exists()


def test_real_stdio_discovery_read_edit_git_and_rejections(task):
    """启动唯一真实服务进程，验证协议 stdout、文件写入及 Git 查询。"""
    _, manager, config = task
    path = manager.artifacts / "task.json"
    path.write_text(config.model_dump_json(), encoding="utf-8")
    with connect(path, 30) as rpc:
        names = {t.name for t in rpc.portal.call(rpc.client.list_tools).tools}
        assert "run_pytest" in names and not names & {"run_command", "run_ruff", "git_commit"}
        service = SearchProxy(manager.root, rpc)
        assert service.find_symbol("add", "calculator.py")
        snap = rpc.call("snapshot", path="calculator.py")
        edit = {"edits": [{"path": "calculator.py", "expected_hash": snap["expected_hash"],
                          "old_text": "return a - b", "new_text": "return a + b"}]}
        assert rpc.call("apply_patch", batch=edit, allowed=["calculator.py"])[0]["applied"]
        assert "return a + b" in rpc.call("git_diff")
        assert rpc.call("git_status")["base_commit"] == config.base_commit
        assert config.base_commit in rpc.call("git_log")
        with pytest.raises(ValueError):
            rpc.call("snapshot", path="tests/test_calculator.py")
        with pytest.raises(ValueError):
            rpc.call("apply_patch", batch=edit, allowed=["other.py"])
    assert "a + b" in (manager.root / "calculator.py").read_text(encoding="utf-8")


def test_bad_schema_and_unknown_tool_are_audited(task):
    """SDK 在进入业务函数前拒绝的调用也必须留下审计，且不写参数正文。"""
    ctx = context(task)
    with start_blocking_portal() as portal:
        with portal.wrap_async_context_manager(Client(create_server(ctx))) as client:
            async def invalid():
                """测试省略必填哈希与不存在的工具，不触发文件副作用。"""
                first = await client.call_tool("apply_patch", {"batch": {"edits": [
                    {"path": "calculator.py", "old_text": "private-secret", "new_text": "a"}]},
                    "allowed": ["calculator.py"]})
                second = await client.call_tool("run_command", {"command": "private-secret"})
                return first, second
            first, second = portal.call(invalid)
    assert first.is_error and second.is_error
    text = ctx.audit.path.read_text(encoding="utf-8")
    assert "rpc.apply_patch" in text and "rpc.unknown_tool" in text
    assert "private-secret" not in text


def test_tool_audit_post_failure_preserves_file(task, monkeypatch):
    """文件已写但结束审计失败必须 blocked，保留文件而非重复应用补丁。"""
    import audit.writer
    ctx = context(task)
    snap = ctx.editor.snapshot("calculator.py")
    batch = EditBatch(edits=[dict(path="calculator.py", expected_hash=snap["expected_hash"],
                                 old_text="return a - b", new_text="return a + b")])
    actual, count = audit.writer.os.fsync, [0]

    def fsync(fd):
        """仅在文件写入成功后的审计持久化失败，编辑器自身 fsync 允许通过。"""
        count[0] += 1
        if count[0] == 3:
            raise OSError("audit failure")
        actual(fd)

    monkeypatch.setattr(audit.writer.os, "fsync", fsync)
    result = ctx.invoke("apply_patch", "Coder", lambda: ctx.apply(batch, ["calculator.py"]))
    assert not result["ok"] and result["blocked"]
    assert "return a + b" in (ctx.workspace.root / "calculator.py").read_text(encoding="utf-8")
    calls = []
    assert not ctx.invoke("write", "Coder", lambda: calls.append(1))["ok"]
    assert not calls


def test_injected_dependencies_must_match_worktree(task):
    """最小注入接口不能把另一个工作区的编辑器或测试器接入本任务。"""
    ctx = context(task)
    bad = SimpleNamespace(workspace=SimpleNamespace(root=ctx.workspace.root.parent), tests=ctx.config.tests)
    with pytest.raises(ValueError, match="同一任务"):
        RepairWorkflow(ctx.workspace, DemoModel(), tests=ctx.config.tests, service=ctx.service, runner=bad)


def test_mcp_cancellation_waits_for_execution_cleanup(task):
    """真实协议取消必须通知执行线程，并等待其 finally 完成后释放任务状态。"""
    _, _, config = task
    cancel, cleaned = Event(), Event()

    def run():
        """模拟可取消的阻塞容器等待；不执行外部命令。"""
        try:
            cancel.wait(3)
            return TestResult(outcome="cancelled")
        finally:
            cleaned.set()

    ctx = ToolContext(config, runner=SimpleNamespace(run=run, cancel=cancel))
    with start_blocking_portal() as portal:
        with portal.wrap_async_context_manager(Client(create_server(ctx))) as client:
            async def request():
                """取消等待中的 MCP 请求，并让服务端有机会完成清理。"""
                with anyio.move_on_after(0.2):
                    await client.call_tool("run_pytest")
                with anyio.fail_after(5):
                    while not cleaned.is_set():
                        await anyio.sleep(0.02)
            portal.call(request)
    assert cancel.is_set() and cleaned.is_set()
