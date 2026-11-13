"""V5 单任务 Worker：只读初始化、持久预算、阶段级多 Agent。"""

import json
import os
from pathlib import Path
import secrets
import sys
from typing import Any

from web_runtime.store import Store


def run(directory: str | Path) -> None:
    """运行到终态或持久审批点，异常保留现场。

    Args:
        directory: 调度器创建的任务目录。
    """
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.types import Command
    from langchain_openai import ChatOpenAI
    from graph.agent_tools import AgentTools
    from graph.autonomous import AutonomousAgents
    from graph.budget import Budget, Limits
    from graph.test_runner import redact_data
    from mcp_client.client import connect
    from security.credentials import credential
    from web_runtime.task_setup import initialize
    import config

    directory = Path(directory)
    store = Store(directory.parent / "tasks.sqlite")
    task_id = directory.name
    data = store.get(task_id)
    options = data["options"]
    budget = Budget(directory / "budget.sqlite", Limits.model_validate(options.get("budget", {})))
    stage, result = "任务初始化", {}
    approval_wait = False
    try:
        task = initialize(directory, options)
        if not task.enforce_roles:
            raise ValueError("旧 V4 任务配置未启用角色认证，请保留现场并新建 V5 任务")
        # 每次 Worker 创建新通道密钥，模型输入和持久产物均不包含它。
        os.environ["AICD_ROLE_SECRET"] = secrets.token_hex(32)
        stage = "初始化模型"
        model = ChatOpenAI(model=options["model"] or config.LLM_MODEL,
            base_url=options["base_url"] or config.LLM_BASE_URL, api_key=credential("LLM"),
            max_tokens=options["max_tokens"], timeout=config.MODEL_TIMEOUT,
            max_retries=config.MODEL_MAX_RETRIES,
            extra_body={"enable_thinking": config.QWEN_ENABLE_THINKING}
            if (options["model"] or config.LLM_MODEL).startswith("qwen") else None)
        stage = "连接 MCP 和 SQLite Checkpointer"
        with connect(directory / "task.json") as rpc, SqliteSaver.from_conn_string(str(directory / "graph.sqlite")) as saver:
            def events(message: str) -> None:
                """保存简短行动说明，取消状态不能被进度覆盖。"""
                if store.get(task_id)["status"] != "running":
                    raise RuntimeError("任务已取消或暂停，停止后续调用")
                store.update(task_id, "running", progress=message, budget=budget.snapshot())

            tools = AgentTools(rpc, directory, task.allowed_files, options["mode"], budget=budget)
            tools.repository = Path(task.workspace)
            tools.tests = task.tests
            agents = AutonomousAgents(model, tools, budget, events, options["context_chars"])
            from memory.experience import ExperienceStore
            agents.experiences = ExperienceStore(directory.parent / "experiences.sqlite").retrieve(options["repository"], options["request"])
            agents.feedback_provider = lambda: store.get(task_id).get("user_feedback", [])
            graph = agents.build(saver)
            cfg = {"configurable": {"thread_id": task_id}, "recursion_limit": 1000}
            snapshot = graph.get_state(cfg)
            if snapshot.values:
                argument: Any = Command(resume=data["decision"])
            else:
                argument = {"user_request": options["request"], "mode": options["mode"],
                    "status": "running", "iteration": 0, "max_iterations": options["max_iterations"],
                    "changes": [], "evidence": options.get("conversation", [])}
            stage = "执行自主 Agent 图"
            for update in graph.stream(argument, cfg, stream_mode="updates"):
                for name, values in update.items():
                    if name == "__interrupt__":
                        continue
                    store.update(task_id, "running", current_node=name, budget=budget.snapshot())
            snapshot = graph.get_state(cfg)
            result = dict(snapshot.values)
            pending = [i for t in snapshot.tasks for i in t.interrupts]
            if pending:
                approval_wait = True
                status = "paused" if store.get(task_id).get("pause_requested") else "waiting"
                store.update(task_id, status, pause_requested=False,
                    approval={"id": pending[0].id, **pending[0].value}, result=redact_data(result),
                    budget=budget.snapshot(), current_node=snapshot.next[0])
                return
            rpc.node = "runtime"
            if task.readonly and result.get("status") == "success":
                # 模型结束与报告发布之间也可能发生源码变化，最终再核验一次。
                from services.citations import validate_citations
                result["citations"] = validate_citations(result["answer"], agents.memory.load()["facts"], Path(task.workspace))
            changes = [] if task.readonly else rpc.call("current_changes")
            diff = "" if task.readonly else rpc.call("git_diff")
            if not task.readonly and changes != result.get("changes", []):
                # 工具返回顺序可能不同，比较规范化后的实际哈希。
                from graph.operations import digest
                if digest(sorted(changes, key=lambda r: r["path"])) != digest(sorted(result.get("changes", []), key=lambda r: r["path"])):
                    result.update(status="blocked", error="测试后源码发生变化，需重新验收")
            report = {"status": result["status"], "changes": changes, "tests": result.get("test_result"),
                "test_history": result.get("test_history", []),
                "review": result.get("review_result"), "error": result.get("error"),
                "citations": result.get("citations", []), "context": getattr(agents.memory, "last_usage", {}),
                "budget": budget.snapshot(), "remaining_issues": [] if result["status"] == "success" else [result.get("error") or "任务未完成"],
                "limits": ["仅验证可见测试；Python 动态调用不保证静态完整性"],
                "diff_path": str(directory / "changes.diff"), "report_path": str(directory / "report.json")}
            if not task.readonly:
                from graph.editor import Workspace
                from sandbox.docker_runner import DockerRunner
                # 清理也是验收的一部分，必须在发布 success 之前完成。
                DockerRunner(Workspace(task.workspace, trusted_copy=True), task.tests, task.image,
                    timeout=task.timeout, task_id=task_id, artifacts=directory).cleanup_remaining()
            result["final_report"] = redact_data(report)
            (directory / "changes.diff").write_text(redact_data(diff), encoding="utf-8")
            (directory / "report.json").write_text(json.dumps(result["final_report"], ensure_ascii=False, indent=2), encoding="utf-8")
            store.update(task_id, result["status"], result=redact_data(result), diff=redact_data(diff),
                answer=redact_data(result.get("answer", "")), approval=None, budget=budget.snapshot())
            store.update(task_id, result["status"], resources_cleaned=True)
    except Exception as exc:
        from web_runtime.diagnostics import record_failure
        failure = record_failure(directory, exc, stage)
        store.update(task_id, "blocked", **failure, budget=budget.snapshot(),
            result=redact_data({**result, "status": "blocked", "error": failure.get("error"), "final_report": {"status": "blocked", "budget": budget.snapshot(),
                "remaining_issues": [failure.get("error", "执行未完成，请检查诊断及副作用账本")]}}))
    finally:
        path = directory / "task.json"
        if path.exists():
            from mcp_servers.context import TaskConfig
            task = TaskConfig.model_validate_json(path.read_text(encoding="utf-8"))
            if not task.readonly and not approval_wait and not store.get(task_id).get("resources_cleaned"):
                from graph.editor import Workspace
                from sandbox.docker_runner import DockerRunner
                try:
                    DockerRunner(Workspace(task.workspace, trusted_copy=True), task.tests, task.image,
                        timeout=task.timeout, task_id=task_id, artifacts=directory).cleanup_remaining()
                except Exception:
                    # 清理失败必须阻止应用，即使业务刚刚成功。
                    store.update(task_id, "blocked", error="容器清理失败，请检查任务审计", expected="running")


if __name__ == "__main__":
    run(sys.argv[1])
