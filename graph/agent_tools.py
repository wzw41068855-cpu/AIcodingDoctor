"""将模型动作转换为受限 MCP 调用，审批由可信宿主执行。"""

import json
from pathlib import Path
from typing import Any

from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict, Field

from graph.permissions import authorize
from graph.state import EditBatch
from graph.operations import digest


class ToolAction(BaseModel):
    """专业 Agent 的一次决策，额外字段一律拒绝。"""

    model_config = ConfigDict(extra="forbid")
    tool: str = Field(default="", description="下一步工具名，或 finish 表示交还当前阶段")
    arguments: dict[str, Any] = Field(default_factory=dict, description="工具参数对象，不包含调用身份")
    summary: str = Field(default="", max_length=4000, description="调用工具的简短目的，或已完成阶段的总结")
    verdict: str = Field(default="", pattern="^(|passed|retry|blocked)$")


class AgentTools:
    """不向模型暴露 RPC 对象、角色凭据或任意执行接口。"""

    def __init__(self, rpc: Any, directory: Path, allowed: list[str],
                 mode: str, approval: bool = True, budget: Any = None) -> None:
        """绑定用户授权和宿主审批策略。

        Args:
            rpc: 已认证的 MCP 通道。
            directory: 工作树外任务目录。
            allowed: 用户授权的业务文件。
            mode: ask 或 repair。
            approval: 正式运行必须启用，离线测试可关闭。
            budget: 可选的内部 RPC 预算账本。
        """
        self.rpc, self.directory, self.allowed = rpc, directory, allowed
        self.mode, self.approval = mode, approval
        self.budget = budget

    def call(self, role: str, action: ToolAction, operation_id: str) -> Any:
        """校验动作后进行预览、审批和实际执行。

        Args:
            role: Graph 阶段绑定的角色，不取自模型参数。
            action: 经模型 schema 校验的动作。
            operation_id: 来自阶段/步数的稳定编号。
        Returns:
            脱敏前的工具业务结果。
        Raises:
            ValueError: 参数越权、审批拒绝或补丁失效。
        """
        authorize(role, action.tool)
        if self.mode == "ask" and role != "Explorer":
            raise ValueError("问答任务仅允许 Explorer 工具")
        arguments = dict(action.arguments)
        if any(key in arguments for key in ("node", "_capability", "_operation_id")):
            raise ValueError("模型不能指定调用身份")
        if action.tool == "run_pytest" and any("::" in t for t in arguments.get("tests", [])):
            raise ValueError("run_pytest 不支持 :: 节点ID，请选授权测试文件或省略 tests 运行全部授权测试")
        if action.tool == "read_artifact":
            from memory.artifacts import Artifacts
            return Artifacts(self.directory).read(**arguments)
        self.rpc.node = role
        if action.tool == "apply_patch":
            if set(arguments) != {"batch"}:
                raise ValueError("补丁动作只接受 batch；路径范围由宿主固定")
            batch = EditBatch.model_validate(arguments["batch"])
            arguments["batch"] = batch.model_dump()
            arguments["allowed"] = self.allowed
            # 重启后仍可重用同一个审批记录；Editor 会重新校验源文件版本。
            key = digest(batch.model_dump())
            approved_path = self.directory / ("approved_" + key + ".json")
            if not approved_path.exists():
                if self.budget:
                    self.budget.reserve("tool_calls", "preview:" + operation_id)
                preview = self.rpc.call("preview_patch", **arguments)
                decision = interrupt({"kind": "patch", **preview}) if self.approval else {"approve": True}
                if not decision.get("approve"):
                    return {"rejected": True, "feedback": decision.get("feedback", "")}
                approved_path.write_text(json.dumps(preview, ensure_ascii=False), encoding="utf-8")
        if action.tool in {"run_pytest", "add_regression_test"} and self.approval:
            decision = interrupt({"kind": "test", "tool": action.tool, "arguments": arguments})
            if not decision.get("approve"):
                return {"rejected": True, "feedback": decision.get("feedback", "")}
        result = self.rpc.call(action.tool, **arguments, _operation_id=operation_id)
        if action.tool == "apply_patch":
            if self.budget:
                self.budget.reserve("tool_calls", "changes:" + operation_id)
            return self.rpc.call("current_changes")
        return result
