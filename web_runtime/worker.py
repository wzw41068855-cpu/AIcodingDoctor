"""单任务子进程：保持 Web 路由与业务、MCP 生命周期分离。"""

import json
import os
from pathlib import Path
import sys
from pydantic_core import to_jsonable_python

from web_runtime.store import Store


def run(directory):
    """运行到完成或下一个审批点；异常记录安全诊断，不保存凭据或 SDK 原文。

    Args:
        directory: 调度器创建的独立任务目录。
    Notes:
        每次恢复重新建立 MCP 会话；SqliteSaver 保存 Graph 状态。
        进程中途崩溃的写入不自动重放，由调度器标记为 blocked。
    """
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.types import Command
    from langchain_openai import ChatOpenAI
    from audit.writer import AuditWriter
    from graph.editor import Workspace
    from graph.nodes import RepairNodes
    from graph.test_runner import redact_data
    from mcp_client.client import connect, SearchProxy, EditorProxy, RunnerProxy
    from mcp_servers.context import TaskConfig
    from sandbox.docker_runner import DockerRunner
    from workspace.manager import WorktreeManager
    from web_runtime.workflow import build_graph
    import config

    directory = Path(directory)
    store = Store(directory.parent / "tasks.sqlite")
    task_id = directory.name
    data = store.get(task_id)
    options = data["options"]
    runner = None
    stage = "任务初始化"
    try:
        task_path = directory / "task.json"
        if not task_path.exists():
            stage = "校验源仓库路径"
            manager = WorktreeManager(Path(options["repository"]), directory,
                                      AuditWriter(directory / "audit.jsonl", task_id))
            stage = "创建隔离 Git worktree"
            root = manager.create()
            stage = "校验工作区和测试路径"
            workspace = Workspace(root, trusted_copy=True)
            runner = DockerRunner(workspace, options["tests"], options["image"],
                                  timeout=options["timeout"], task_id=task_id, artifacts=directory)
            stage = "检查本地 Docker 镜像"
            image = runner.preflight() if options["mode"] == "repair" else options["image"]
            task = TaskConfig(task_id=task_id, workspace=str(root), artifacts=str(directory),
                base_commit=manager.base, allowed_files=options["allowed_files"], tests=options["tests"],
                image=image, timeout=options["timeout"], trusted_copy=True)
            task_path.write_text(task.model_dump_json(), encoding="utf-8")
        stage = "加载任务配置"
        task = TaskConfig.model_validate_json(task_path.read_text(encoding="utf-8"))
        workspace = Workspace(task.workspace, trusted_copy=True)
        runner = DockerRunner(workspace, task.tests, task.image, timeout=task.timeout,
                              task_id=task_id, artifacts=directory)
        stage = "初始化模型"
        model = ChatOpenAI(model=options["model"] or config.LLM_MODEL,
            base_url=options["base_url"] or config.LLM_BASE_URL,
            api_key=os.environ.get("AICD_WEB_API_KEY") or os.environ.get("DASHSCOPE_API_KEY"),
            max_tokens=options["max_tokens"], timeout=60, max_retries=1)
        stage = "连接 MCP 和 SQLite Checkpointer"
        with connect(task_path) as rpc, SqliteSaver.from_conn_string(str(directory / "graph.sqlite")) as saver:
            service = SearchProxy(workspace.root, rpc)

            def events(message):
                """保存节点进度并维持 V3 审计节点归因。"""
                for name in ("Planner", "Coder", "Tester", "Reviewer", "Finalizer"):
                    if message.startswith("进入 " + name):
                        rpc.node = name
                store.update(task_id, "running", progress=message, current_node=rpc.node)

            if options["mode"] == "ask":
                stage = "只读代码问答"
                evidence = service.search_code(options["request"], top_k=3)
                answer = model.invoke([("system", "你是只读代码问答助手。根据检索证据回答，不执行修改。"),
                    ("user", json.dumps({"question": options["request"], "evidence": [r.model_dump() for r in evidence]}, ensure_ascii=False))])
                store.update(task_id, "success", answer=redact_data(str(answer.content)))
                return
            nodes = RepairNodes(service, EditorProxy(workspace, rpc), RunnerProxy(workspace, task.tests, rpc), model, directory, events)
            nodes.max_prompt_chars = options.get("context_chars", 24000)
            graph = build_graph(nodes, saver)
            cfg = {"configurable": {"thread_id": task_id}, "recursion_limit": 200}
            snapshot = graph.get_state(cfg)
            if snapshot.values:
                argument = Command(resume=data["decision"])
            else:
                argument = dict(task_id=task_id, user_request=options["request"], workspace_root=task.workspace,
                    status="running", iteration=0, max_iterations=options["max_iterations"], changes=[], index_dirty=False)
            stage = "执行修复图"
            # updates 流按节点提供输出；保存节点名、轮次及结果，刷新页面后
            # 仍可区分 Planner 计划、Coder 修改、Tester 测试与 Reviewer 判断。
            for update in graph.stream(argument, cfg, stream_mode="updates"):
                for name, values in update.items():
                    if name == "__interrupt__":
                        continue
                    outputs = store.get(task_id).get("node_results", {})
                    outputs[name] = redact_data(to_jsonable_python(values))
                    store.update(task_id, "running", current_node=name, node_results=outputs)
            snapshot = graph.get_state(cfg)
            result = snapshot.values
            pending = [i for t in snapshot.tasks for i in t.interrupts]
            waiting_status = "paused" if store.get(task_id).get("pause_requested") else "waiting"
            store.update(task_id, waiting_status if pending else result["status"],
                pause_requested=False,
                approval={"id": pending[0].id, **pending[0].value} if pending else None,
                result=redact_data(to_jsonable_python(result)), diff=nodes.editor.diff(),
                current_node=snapshot.next[0] if snapshot.next else "Finalizer")
    except Exception as exc:
        from web_runtime.diagnostics import record_failure
        store.update(task_id, "blocked", **record_failure(directory, exc, stage))
    finally:
        if runner:
            try:
                runner.cleanup_remaining()
            except Exception:
                store.update(task_id, "blocked", error="容器清理失败，请检查任务审计")


if __name__ == "__main__":
    run(sys.argv[1])
