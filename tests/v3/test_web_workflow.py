"""V4 审批图经真实 MCP 协议执行离线修复闭环。"""

from contextlib import ExitStack
from types import SimpleNamespace

import pytest
from anyio.from_thread import start_blocking_portal
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from mcp import Client

from graph.demo import DemoModel
from graph.nodes import RepairNodes
from graph.state import TestResult
from mcp_client.client import ToolRPC, SearchProxy, EditorProxy, RunnerProxy
from mcp_servers.context import ToolContext
from mcp_servers.server import create_server
from web_runtime.workflow import build_graph
from web_runtime.store import Store


@pytest.mark.parametrize("mode,expected", [("success", "success"), ("retry", "success"), ("limit", "failed")])
def test_persistent_web_graph_with_mcp(task, mode, expected):
    """每个审批点关闭会话与数据库，再构建图恢复；不调用真实模型或 Docker。"""
    repo, manager, config = task
    model = DemoModel(mode)
    store = Store(manager.artifacts / "view.sqlite")
    store.create(config.task_id, {})
    cfg = {"configurable": {"thread_id": config.task_id}, "recursion_limit": 100}
    argument = dict(task_id=config.task_id, user_request="修复 calculator.py 的 add",
        workspace_root=config.workspace, status="running", iteration=0, max_iterations=3,
        changes=[], index_dirty=False)
    approvals = []
    for _ in range(15):
        def run():
            """可信 fixture 结果替身，不在宿主执行目标仓库代码。"""
            correct = "return a + b\n" in (manager.root / "calculator.py").read_text(encoding="utf-8")
            return TestResult(outcome="passed" if correct else "failed", returncode=0 if correct else 1,
                              passed=int(correct), failed=int(not correct))
        ctx = ToolContext(config, runner=SimpleNamespace(run=run))
        with ExitStack() as stack:
            portal = stack.enter_context(start_blocking_portal())
            client = stack.enter_context(portal.wrap_async_context_manager(Client(create_server(ctx))))
            rpc = ToolRPC(portal, client, 20)
            saver = stack.enter_context(SqliteSaver.from_conn_string(str(manager.artifacts / "graph.sqlite")))
            nodes = RepairNodes(SearchProxy(manager.root, rpc), EditorProxy(ctx.workspace, rpc),
                RunnerProxy(ctx.workspace, config.tests, rpc), model, manager.artifacts)
            graph = build_graph(nodes, saver)
            for update in graph.stream(argument, cfg, stream_mode="updates"):
                for name, values in update.items():
                    if name != "__interrupt__":
                        store.update(config.task_id, "running", current_node=name, node_result=values)
            snapshot = graph.get_state(cfg)
            result = snapshot.values
            store.update(config.task_id, "waiting" if snapshot.next else result["status"], result=result)
            pending = [i for t in snapshot.tasks for i in t.interrupts]
            if not pending:
                break
            approvals.append(pending[0].value["kind"])
            if pending[0].value["kind"] == "plan":
                assert "return a - b" in (manager.root / "calculator.py").read_text(encoding="utf-8")
            argument = Command(resume={"approve": True})
    assert result["status"] == expected, result
    assert approvals[:3] == ["plan", "edit", "test"]
    assert result["final_report"]["changes"]
    assert store.get(config.task_id)["result"]["changes"][0]["path"] == "calculator.py"
    assert "return a - b" in (repo / "calculator.py").read_text(encoding="utf-8")
