"""自主阶段、专业循环、完成硬规则与持久预算验证。"""

import json
from pathlib import Path
from types import SimpleNamespace

from langgraph.checkpoint.memory import InMemorySaver
import pytest

from graph.agent_tools import ToolAction
from graph.autonomous import AutonomousAgents, Schedule
from graph.budget import Budget, BudgetExceeded, Limits
from graph.operations import digest
from graph.permissions import ROLE_TOOLS, authorize


class ScriptModel:
    """按预定轨迹返回动作，模型调用仍经过真实预算和 schema。"""

    def __init__(self, script: list[dict]) -> None:
        """保存脚本与角色调用记录。"""
        self.script, self.calls = iter(script), []

    def with_structured_output(self, schema: type, **kwargs: object) -> SimpleNamespace:
        """提供生产模型兼容接口。"""
        def invoke(messages: list, config: dict | None = None) -> dict:
            """校验每个脚本决策并记录角色。"""
            self.calls.append("Supervisor" if schema is Schedule else "Agent")
            return next(self.script)
        return SimpleNamespace(invoke=invoke)


class FakeTools:
    """验证阶段调用顺序，不接触业务源码。"""

    allowed: list[str] = []

    def __init__(self) -> None:
        """初始化调用轨迹。"""
        self.calls = []

    def call(self, role: str, action: ToolAction, operation_id: str) -> dict:
        """复用生产权限检查。"""
        authorize(role, action.tool)
        self.calls.append((role, action.tool))
        return {"files": ["main.py"]}


def test_three_tools_inside_one_explorer_phase(tmp_path: Path) -> None:
    """三个工具之间没有 Supervisor 调用，问答路径动态结束。"""
    model = ScriptModel([{"next_role": "Explorer", "reason": "读取证据"},
        {"tool": "list_files"}, {"tool": "read_file", "arguments": {"path": "main.py"}},
        {"tool": "find_symbol", "arguments": {"name": "login"}},
        {"tool": "finish", "summary": "main.py:1 登录函数"}, {"next_role": "finish", "reason": "已找到证据"}])
    tools = FakeTools()
    budget = Budget(tmp_path / "budget.sqlite")
    graph = AutonomousAgents(model, tools, budget).build(InMemorySaver())
    result = graph.invoke({"user_request": "解释登录", "mode": "ask", "status": "running",
        "max_iterations": 3}, {"configurable": {"thread_id": "ask"}})
    assert result["status"] == "success"
    assert model.calls == ["Supervisor", "Agent", "Agent", "Agent", "Agent", "Supervisor"]
    assert len(tools.calls) == 3
    assert Budget(tmp_path / "budget.sqlite").snapshot()["tool_calls"] == 3


@pytest.mark.parametrize("kind", ["model_calls", "tool_calls"])
def test_call_budget_survives_restart(tmp_path: Path, kind: str) -> None:
    """次数预留和停止原因跨重启保持。"""
    path = tmp_path / "budget.sqlite"
    budget = Budget(path, Limits(**{kind: 1}))
    budget.reserve(kind, "one")
    Budget(path).reserve(kind, "one")
    with pytest.raises(BudgetExceeded):
        Budget(path).reserve(kind, "two")
    assert Budget(path).snapshot()["stop_reason"] == kind


def test_token_and_elapsed_budgets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """真实 Token 和累计壁钟均有独立硬限制。"""
    budget = Budget(tmp_path / "tokens.sqlite", Limits(total_tokens=2))
    budget.record_tokens(None)
    budget.record_tokens(2)
    with pytest.raises(BudgetExceeded):
        budget.reserve("tool_calls", "one")
    clock = budget.snapshot()["started"]
    monkeypatch.setattr("graph.budget.time.time", lambda: clock + 1000)
    with pytest.raises(BudgetExceeded):
        budget.reserve("model_calls", "two")


@pytest.mark.parametrize("role", list(ROLE_TOOLS))
def test_role_permission_matrix(role: str) -> None:
    """每种角色对所有工具逐项验证白名单。"""
    universe = set().union(*ROLE_TOOLS.values()) | {"shell", "delete_file"}
    for tool in universe:
        if tool in ROLE_TOOLS[role]:
            authorize(role, tool)
        else:
            with pytest.raises(ValueError):
                authorize(role, tool)


@pytest.mark.parametrize("outcome", ["failed", "no_tests", "timeout", "environment", "cancelled"])
def test_model_pass_cannot_bypass_test_result(outcome: str) -> None:
    """模型声称通过不能越过真实测试硬规则。"""
    state = {"changes": [{"path": "main.py"}], "test_result": {"outcome": outcome,
        "returncode": 0, "passed": 1}, "review_result": {"decision": "passed"}}
    state["test_version"] = digest(state["changes"])
    assert not AutonomousAgents.can_finish(state)


@pytest.mark.parametrize("limits,actions,reason", [
    (Limits(stage_steps=1), [{"tool": "list_files"}], "阶段工具步数"),
    (Limits(no_progress=1), [{"tool": "list_files"}, {"tool": "list_files"}], "重复无进展"),
])
def test_stage_and_repetition_limits(tmp_path: Path, limits: Limits, actions: list[dict], reason: str) -> None:
    """专业 Agent 无法通过连续调用绕开阶段和重复阈值。"""
    model = ScriptModel([{"next_role": "Explorer", "reason": "探索"}, *actions])
    tools = FakeTools()
    result = AutonomousAgents(model, tools, Budget(tmp_path / "b.sqlite", limits)).build().invoke(
        {"mode": "ask", "user_request": "解释", "status": "running", "max_iterations": 1})
    assert result["status"] == "blocked" and reason in result["error"]
    assert len(tools.calls) == 1


def test_repair_iteration_limit_stops_before_coder(tmp_path: Path) -> None:
    """超过修复轮数后不能进入下一次 Coder。"""
    model = ScriptModel([{"next_role": "Coder", "reason": "重试"}])
    result = AutonomousAgents(model, FakeTools(), Budget(tmp_path / "b.sqlite")).build().invoke(
        {"mode": "repair", "user_request": "修复", "status": "running", "iteration": 1, "max_iterations": 1})
    assert result["status"] == "failed" and model.calls == ["Supervisor"]


def test_invalid_model_output_is_corrected_once_within_budget(tmp_path: Path) -> None:
    """模型暂时不输出结构化调用时，允许一次纠正但不猜测动作。"""
    model = ScriptModel([{"next_role": "Explorer", "reason": "读取"}, None,
        {"tool": "read_file", "arguments": {"path": "main.py"}}, {"summary": "main.py:1 证据"},
        {"next_role": "finish", "reason": "已回答"}])
    tools = FakeTools()
    result = AutonomousAgents(model, tools, Budget(tmp_path / "b.sqlite")).build().invoke(
        {"mode": "ask", "user_request": "解释", "status": "running", "max_iterations": 1})
    assert result["status"] == "success" and len(tools.calls) == 1


def test_continuously_invalid_model_output_stops_without_tools(tmp_path: Path) -> None:
    """连续无效返回后明确受阻，不继续调度或执行工具。"""
    model = ScriptModel([None, None])
    tools = FakeTools()
    result = AutonomousAgents(model, tools, Budget(tmp_path / "b.sqlite")).build().invoke(
        {"mode": "ask", "user_request": "解释", "status": "running", "max_iterations": 1})
    assert result["status"] == "blocked" and len(model.calls) == 2 and not tools.calls


def test_coder_does_not_return_after_only_a_summary(tmp_path: Path) -> None:
    """只声称读完文件不能让 Coder 提前结束修复阶段。"""
    model = ScriptModel([{"next_role": "Coder", "reason": "修复"}, {"summary": "准备读取代码"},
        {"tool": "snapshot", "arguments": {"path": "main.py"}},
        {"summary": "需要更多定位证据", "verdict": "retry"}, {"next_role": "blocked", "reason": "证据不足"}])
    tools = FakeTools()
    result = AutonomousAgents(model, tools, Budget(tmp_path / "b.sqlite")).build().invoke(
        {"mode": "repair", "user_request": "修复", "status": "running", "max_iterations": 3})
    assert result["status"] == "blocked"
    assert model.calls == ["Supervisor", "Agent", "Agent", "Agent", "Supervisor"]
    assert tools.calls == [("Coder", "snapshot")]


def test_dynamic_failure_reexploration_repair_and_review(tmp_path: Path) -> None:
    """测试失败可再次探索并修复，成功后必须独立读取 Diff 审查。"""
    script = [
        {"next_role": "Explorer", "reason": "定位"}, {"tool": "read_file", "arguments": {"path": "main.py"}}, {"summary": "找到根因"},
        {"next_role": "Coder", "reason": "首次修改"}, {"tool": "apply_patch"}, {"tool": "sync_changed_files", "arguments": {"paths": ["main.py"]}},
        {"next_role": "Tester", "reason": "验证"}, {"tool": "run_pytest"},
        {"next_role": "Explorer", "reason": "依据失败重新定位"}, {"tool": "read_file", "arguments": {"path": "other.py"}}, {"summary": "发现边界条件"},
        {"next_role": "Coder", "reason": "修复边界"}, {"tool": "apply_patch"}, {"tool": "sync_changed_files", "arguments": {"paths": ["main.py"]}},
        {"next_role": "Tester", "reason": "回归"}, {"tool": "run_pytest"},
        {"next_role": "Reviewer", "reason": "独立审查"}, {"tool": "git_diff"}, {"tool": "current_changes"}, {"summary": "符合需求与可见测试", "verdict": "passed"},
        {"next_role": "finish", "reason": "证据齐全"}]
    class Tools(FakeTools):
        """模拟真实测试失败与第二次修复后的结果。"""
        def __init__(self) -> None:
            super().__init__()
            self.rounds, self.records = 0, []
        def call(self, role: str, action: ToolAction, operation_id: str):
            super().call(role, action, operation_id)
            if action.tool == "apply_patch":
                self.rounds += 1
                self.records = [{"path": "main.py", "before_hash": "a" * 64,
                                 "after_hash": str(self.rounds) * 64, "applied": True}]
                return self.records
            if action.tool == "current_changes":
                return self.records
            if action.tool == "run_pytest":
                passed = self.rounds == 2
                return {"outcome": "passed" if passed else "failed", "returncode": 0 if passed else 1,
                        "passed": int(passed), "failed": int(not passed)}
            if action.tool == "git_diff":
                return "--- a/main.py\n+++ b/main.py\n-old\n+fixed\n"
            return {}
    tools = Tools()
    result = AutonomousAgents(ScriptModel(script), tools, Budget(tmp_path / "b.sqlite")).build().invoke(
        {"mode": "repair", "user_request": "修复", "status": "running", "max_iterations": 3},
        {"recursion_limit": 100})
    assert result["status"] == "success" and result["iteration"] == 2
    assert [r["outcome"] for r in result["test_history"]] == ["failed", "passed"]
    assert tools.calls[-2:] == [("Reviewer", "git_diff"), ("Reviewer", "current_changes")]
