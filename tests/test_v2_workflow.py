"""注入模拟模型和真实 V1 内存索引验证 Graph 各分支，不访问 API。"""

import json
from pathlib import Path

import pytest

from graph.demo import DemoModel, create_demo, demo_service
from graph.editor import Workspace
from graph.state import EditBatch, ReviewResult, TaskPlan, TestResult
from graph.workflow import RepairWorkflow


@pytest.fixture
def setup_flow(tmp_path):
    """构造完整可信副本和实际 V1 增量服务。"""
    root = tmp_path / "workspace"
    create_demo(root)
    service = demo_service(root, tmp_path)

    def build(model=None, **kwargs):
        return RepairWorkflow(Workspace(root, trusted_copy=True), model or DemoModel(), tests=["tests"],
                              service=service, **kwargs)
    return root, service, build


@pytest.mark.parametrize("mode,status,rounds", [("success", "success", 1), ("retry", "success", 2),
                                              ("limit", "failed", 3)])
def test_graph_real_loop(setup_flow, mode, status, rounds):
    """实际写文件、同步索引和运行 pytest，验证一次成功、反馈重试和上限。"""
    root, service, build = setup_flow
    flow = build(DemoModel(mode))
    state = flow.run("修复 add 加法")
    assert state["status"] == status, state["final_report"]
    assert state["iteration"] == rounds
    report = state["final_report"]
    assert Path(report["diff_path"]).read_text(encoding="utf-8").startswith("--- a/calculator.py")
    assert json.loads(Path(report["report_path"]).read_text(encoding="utf-8"))["status"] == status
    assert sum("进入 Coder" in event for event in flow.nodes.events) == rounds
    assert all(any(f"进入 {node}" in event for event in flow.nodes.events)
               for node in ["Planner", "Coder", "Tester", "Reviewer", "Finalizer"])
    if status == "success":
        assert state["test_result"].passed == 1
        assert "return a + b\n" in service.get_code_context("calculator.py").content
        assert any("return a + b\n" in doc.page_content for doc in service.manager.vector_store.docs.values())


def test_reviewer_blocks_environment_and_never_accepts_failed_tests(setup_flow, monkeypatch):
    """模拟模型声称通过不能越过失败、超时、无测试等硬规则。"""
    _, _, build = setup_flow
    for outcome, expected in [("failed", "retry"), ("timeout", "blocked"), ("no_tests", "blocked"),
                              ("environment", "blocked"), ("cancelled", "blocked")]:
        flow = build()
        state = {"changes": [object()], "test_result": TestResult(outcome=outcome),
                 "iteration": 1, "status": "running"}
        assert flow.nodes.reviewer(state)["review_result"].decision == expected


def test_index_failure_reports_changed_source(setup_flow, monkeypatch):
    """同步异常保留已修改源码，并且不能误报 success。"""
    root, service, build = setup_flow
    monkeypatch.setattr(service, "sync_changed_files", lambda paths: (_ for _ in ()).throw(RuntimeError()))
    state = build().run("修复 add")
    assert state["status"] == "blocked" and state["index_dirty"]
    assert "return a + b" in (root / "calculator.py").read_text(encoding="utf-8")
    assert "索引待重建" in state["final_report"]["error"]


@pytest.mark.parametrize("bad", ["empty", "illegal", "unknown", "invalid", "blocked"])
def test_planner_invalid_or_blocked_plan(setup_flow, bad):
    """空计划、非法路径、无证据文件、无效结构和显式阻塞都有限退出。"""
    _, _, build = setup_flow

    class BadPlanner(DemoModel):
        """仅修改 Planner 返回值的模拟模型。"""

        def _answer(self, schema, messages):
            if schema is TaskPlan:
                if bad == "invalid":
                    return {"unknown": "bad"}
                plan = super()._answer(schema, messages).model_dump()
                if bad == "empty":
                    plan["steps"] = []
                elif bad == "illegal":
                    plan["target_files"] = ["../outside.py"]
                elif bad == "unknown":
                    plan["target_files"] = ["unknown.py"]
                else:
                    plan["blocked_reason"] = "需求越权"
                return plan
            return super()._answer(schema, messages)

    state = build(BadPlanner()).run("修复 add")
    assert state["status"] == "blocked" and state["iteration"] == 0
    assert state["final_report"]["changes"] == []


def test_retrieval_failure_and_service_mismatch(setup_flow, monkeypatch, tmp_path):
    """已有检索配置失效不能假装成功；另一仓库服务不能注入本任务。"""
    root, service, build = setup_flow
    monkeypatch.setattr(service, "search_code", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError()))
    assert build().run("修复 add")["status"] == "blocked"
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(ValueError):
        RepairWorkflow(Workspace(other, trusted_copy=True), DemoModel(), tests=["tests"], service=service)


def test_reviewer_blocked_and_invalid_coder(setup_flow):
    """模型审查阻塞和 Coder 无效结构均通过 Finalizer 返回报告。"""
    _, _, build = setup_flow

    class BlockReview(DemoModel):
        """返回明确阻塞结果的审查替身。"""

        def _answer(self, schema, messages):
            if schema is ReviewResult:
                return ReviewResult(decision="blocked", reason="可见测试不足")
            return super()._answer(schema, messages)

    assert build(BlockReview()).run("修复 add")["status"] == "blocked"


def test_invalid_model_output_each_node(setup_flow, monkeypatch):
    """Planner/Coder/Reviewer 结构错误均有限终止且写最终报告。"""
    _, _, build = setup_flow
    for failed_schema in [TaskPlan, EditBatch, ReviewResult]:
        class InvalidModel(DemoModel):
            """在指定节点返回非法数据，其他节点沿用固定策略。"""

            def _answer(self, schema, messages):
                if schema is failed_schema:
                    return {"unexpected": "invalid"}
                return super()._answer(schema, messages)

        flow = build(InvalidModel())
        # 各场景使用同一 fixture 时重置副本和索引到已知原始状态。
        root = flow.workspace.root
        original = '"""演示"""\ndef add(a, b):\n    return a - b\n'
        (root / "calculator.py").write_text(original, encoding="utf-8")
        state = flow.run("修复 add")
        assert state["status"] == "blocked" and "final_report" in state


def test_tool_exception_and_cancel_produce_report(setup_flow, monkeypatch):
    """Coder 工具异常、Tester 异常和取消都不会卡在 Graph 内。"""
    _, _, build = setup_flow
    for name, error in [("apply", OSError()), ("run", RuntimeError()), ("run", KeyboardInterrupt())]:
        flow = build()
        (flow.workspace.root / "calculator.py").write_text(
            "def add(a, b):\n    return a - b\n", encoding="utf-8")
        owner = flow.nodes.editor if name == "apply" else flow.nodes.runner
        monkeypatch.setattr(owner, name, lambda *a, _error=error, **kw: (_ for _ in ()).throw(_error))
        state = flow.run("修复 add")
        assert state["status"] == "blocked" and "final_report" in state


def test_repeated_rejected_patch_stops_early(setup_flow):
    """连续输出同一冲突补丁时不盲目重复到全部轮次。"""
    _, _, build = setup_flow

    class ConflictModel(DemoModel):
        """始终引用错误原始哈希的模型。"""

        def _answer(self, schema, messages):
            result = super()._answer(schema, messages)
            if schema is EditBatch:
                result["edits"][0]["expected_hash"] = "0" * 64
            return result

    state = build(ConflictModel()).run("修复 add")
    assert state["status"] == "blocked" and state["iteration"] == 2
    assert "重复补丁" in state["error"]


def test_request_and_iteration_limits(setup_flow):
    """拒绝空需求、超长请求、重复运行和非法轮数。"""
    _, _, build = setup_flow
    for rounds in [0, 6, True]:
        with pytest.raises(ValueError):
            build(max_iterations=rounds)
    flow = build()
    for request in [" ", "x" * 4001]:
        with pytest.raises(ValueError):
            flow.run(request)
    flow.run("修复 add")
    with pytest.raises(ValueError):
        flow.run("再次修复")
