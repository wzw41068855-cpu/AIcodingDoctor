"""统一修复图拓扑：V4 Web 启用审批；离线测试可关闭审批。

职责只包含节点连接和条件路由。模型、检索、Editor、Tester 在外部注入。
"""

from pathlib import Path
import tempfile
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from graph.editor import Editor, Workspace
from graph.nodes import RepairNodes
from graph.state import AgentState
from graph.test_runner import PytestRunner


def after_planner(state: AgentState) -> str:
    """规划可执行才继续，否则收敛到 Finalizer。"""
    return "Coder" if state["status"] == "running" else "Finalizer"


def after_coder(state: AgentState) -> str:
    """补丁拒绝由 Reviewer 判断是否重试，工具错误则终止。"""
    if state["status"] != "running":
        return "Finalizer"
    return "Reviewer" if state.get("patch_error") else "Tester"


def after_tester(state: AgentState) -> str:
    return "Reviewer" if state["status"] == "running" else "Finalizer"


def after_reviewer(state: AgentState) -> str:
    review = state.get("review_result")
    if (state["status"] == "running" and review and review.decision == "retry"
            and state["iteration"] < state["max_iterations"]):
        return "Coder"
    return "Finalizer"


def build_repair_graph(nodes, saver=None, *, require_approval: bool = True):
    """唯一 Graph 工厂。

    Args:
        nodes: 已注入工作区、检索、编辑与测试依赖的 RepairNodes。
        saver: V4 的 SQLite Checkpointer；离线图可以不提供。
        require_approval: True 为 V4 的计划/编辑/测试审批流程；False 用于离线测试。
    """
    builder = StateGraph(AgentState)
    for name in ("Planner", "Coder", "Tester", "Reviewer", "Finalizer"):
        builder.add_node(name, nodes.guarded(name, getattr(nodes, name.lower())))

    if require_approval:
        # interrupt 之前没有副作用；保持原 V4 计划驳回、重规划和恢复行为。
        def plan_gate(state):
            answer = interrupt({"kind": "plan", "plan": state["plan"].model_dump()})
            if answer["approve"]:
                return Command(goto="EditApproval")
            feedback = answer.get("feedback", "").strip()
            if feedback:
                return Command(update={"user_request": (
                    state["user_request"] + "\n用户修改意见：" + feedback)[-4000:]}, goto="Planner")
            return Command(update={"status": "blocked", "error": "用户拒绝计划"}, goto="Finalizer")

        def edit_gate(state):
            answer = interrupt({"kind": "edit", "files": state["plan"].target_files,
                                "iteration": state.get("iteration", 0) + 1})
            return Command(goto="Coder" if answer["approve"] else "Finalizer",
                           update={} if answer["approve"] else
                           {"status": "blocked", "error": "用户拒绝修改"})

        def test_gate(state):
            answer = interrupt({"kind": "test", "command": ["docker", "pytest", *nodes.runner.tests]})
            return Command(goto="Tester" if answer["approve"] else "Finalizer",
                           update={} if answer["approve"] else
                           {"status": "blocked", "error": "用户拒绝测试"})

        builder.add_node("PlanApproval", plan_gate)
        builder.add_node("EditApproval", edit_gate)
        builder.add_node("TestApproval", test_gate)
        planner_next = lambda state: "PlanApproval" if state["status"] == "running" else "Finalizer"
        coder_next = lambda state: "TestApproval" if after_coder(state) == "Tester" else after_coder(state)
        reviewer_next = lambda state: "EditApproval" if after_reviewer(state) == "Coder" else "Finalizer"
    else:
        planner_next = after_planner
        coder_next = after_coder
        reviewer_next = after_reviewer

    builder.add_edge(START, "Planner")
    builder.add_conditional_edges("Planner", planner_next)
    builder.add_conditional_edges("Coder", coder_next)
    builder.add_conditional_edges("Tester", after_tester)
    builder.add_conditional_edges("Reviewer", reviewer_next)
    builder.add_edge("Finalizer", END)
    return builder.compile(checkpointer=saver)


class RepairWorkflow:
    """供离线回归测试使用的 V2 兼容外观；正式 V4 使用 build_repair_graph。"""

    def __init__(self, workspace: Workspace, model, *, tests: list[str], service=None,
                 max_iterations: int = 3, timeout: float = 30, event_sink=None,
                 editor=None, runner=None):
        if type(max_iterations) is not int or not 1 <= max_iterations <= 5:
            raise ValueError("max_iterations 必须在 1 到 5 之间")
        service = workspace.service if service is None else service
        if Path(service.root).resolve() != workspace.root:
            raise ValueError("检索服务必须绑定同一副本")
        self.workspace, self.max_iterations, self.used = workspace, max_iterations, False
        editor = Editor(workspace) if editor is None else editor
        runner = PytestRunner(workspace, tests, timeout=timeout) if runner is None else runner
        if (Path(editor.workspace.root).resolve() != workspace.root
                or Path(runner.workspace.root).resolve() != workspace.root
                or runner.tests != tests):
            raise ValueError("编辑器、测试器和测试集必须绑定同一任务副本")
        self.nodes = RepairNodes(service, editor, runner, model,
                                 Path(tempfile.mkdtemp(prefix="v2-report-")), event_sink)
        self.graph = build_repair_graph(self.nodes, require_approval=False)

    def run(self, request: str) -> AgentState:
        """离线、单次运行，保持历史测试的状态和递归上限。"""
        if self.used or not request.strip() or len(request) > 4000:
            raise ValueError("请求必须非空且不超过 4000 字符；工作流实例只能执行一次")
        self.used = True
        return self.graph.invoke({"task_id": uuid4().hex, "user_request": request,
                                  "workspace_root": str(self.workspace.root), "status": "running",
                                  "iteration": 0, "max_iterations": self.max_iterations,
                                  "changes": [], "index_dirty": False},
                                 config={"recursion_limit": 4 * self.max_iterations + 10})
