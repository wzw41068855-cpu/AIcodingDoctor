"""修复图的结构化输入与状态；字段采用覆盖语义，避免重试重复累加。"""

from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field


class TaskPlan(BaseModel):
    """Planner 输出 TaskPlan，目标文件仍须经过工作区访问控制。

    Attributes:
        target_files: 最多八个已有 Python 文件；不能授权修改测试或配置。
        blocked_reason: 无法执行时的原因，此时不进入 Coder。
        eg：
        {
            "summary": "修复用户登录异常",
            "target_files": ["services/user.py", "services/auth.py"],
            "steps": [
                "分析登录异常的根本原因",
                "修改用户认证逻辑，确保密码验证正确",
                "更新相关单元测试，覆盖新逻辑"
            ],
            "acceptance_criteria": [
                "用户能够正常登录",
                "所有相关单元测试通过"
            ],
            "test_strategy": "使用 pytest 运行现有测试，并添加新的测试用例覆盖修改的逻辑",
            "risks": [
                "可能影响现有用户认证流程",
                "需要确保新逻辑不会引入安全漏洞"
            ],
            "blocked_reason": null
        }
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    summary: str = Field(min_length=1, max_length=2000)# 计划摘要（必填）
    target_files: list[str] = Field(default_factory=list, max_length=8)# 目标修改文件列表
    steps: list[str] = Field(default_factory=list, max_length=12)# 计划步骤列表
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=12)# 计划验收标准列表
    test_strategy: str = Field(default="", max_length=2000)# 计划测试策略说明
    risks: list[str] = Field(default_factory=list, max_length=8)# 计划风险列表
    blocked_reason: str | None = Field(default=None, max_length=2000)# 计划被阻塞的原因，若有则不进入 Coder


class TextEdit(BaseModel):
    """单文件唯一文本替换，expected_hash 为读取时原始字节的 SHA256。
    表示一个文件的单次修改，包含旧文本和新文本。
    Notes:
        每轮每个文件只接受一个替换块；old_text 必须恰好出现一次。
    eg:
    edit = TextEdit(
        path="calculator.py",
        expected_hash="a" * 64,  # 仅作格式演示，并非真实文件哈希
        old_text="return a - b",
        new_text="return a + b",
)
    """

    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=500)# 待编辑文件路径：
    expected_hash: str = Field(pattern=r"^[a-f0-9]{64}$")# 读取文件时的原始内容哈希（SHA256，小写十六进制）：
    old_text: str = Field(min_length=1, max_length=65536)# 期望被替换的旧文本：
    new_text: str = Field(max_length=65536)# 替换后的新文本：


class EditBatch(BaseModel):
    """Coder 输出 EditBatch，包含一轮提交的有限替换列表，空列表属于无效补丁。
     EditBatch：一轮提交多个替换
"""

    model_config = ConfigDict(extra="forbid")
    edits: list[TextEdit] = Field(min_length=1, max_length=8)
    # 编辑列表，包含一轮提交的有限替换


class ChangeRecord(BaseModel):
    """Editor 才实际修改，coder仅制定修改计划，
    Editor生成 ChangeRecord。
    成功应用的文件记录；源码和完整 Diff 不积累在 Graph State 中。"""

    path: str# 文件路径：
    before_hash: str# 修改前的文件哈希（SHA256，小写十六进制）：
    after_hash: str# 修改后的文件哈希（SHA256，小写十六进制）：
    applied: bool = True# 是否成功应用


class TestResult(BaseModel):
    """确定性 pytest 结果；passed 要求退出码为零且至少一个测试实际通过。"""

    __test__ = False#表示这个类不是测试类。
    # 测试执行结果的归一化状态：
    # - passed: 测试通过
    # - failed: 测试失败
    # - no_tests: 未发现测试
    # - timeout: 执行超时
    # - environment: 环境异常（依赖/解释器/路径等）
    # - cancelled: 执行被取消
    outcome: Literal["passed", "failed", "no_tests", "timeout", "environment", "cancelled"]
    # 实际执行的命令及参数（分词后的列表形式），默认空列表。
    command: list[str] = Field(default_factory=list)
    # 进程退出码；None 表示未执行或未拿到退出码。
    returncode: int | None = None
    # 是否发生超时；用于与 outcome 联合判断执行终态。
    timed_out: bool = False
    # 测试通过的数量
    passed: int = 0
    # 测试失败的数量
    failed: int = 0
    # 测试跳过的数量
    skipped: int = 0
    # 测试输出
    output: str = ""
    # 输出是否被截断
    truncated: bool = False
    # 日志文件路径
    log_path: str | None = None


class ReviewResult(BaseModel):
    """Reviewer 决策；模型 passed 仍须通过确定性硬规则。
    eg：
    review = ReviewResult(
        decision="retry",
        reason="pytest 存在失败断言",
        feedback="add 函数结果比预期大 1，请修复偏移量",
        remaining_issues=["test_add 仍然失败"],
)
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    decision: Literal["passed", "retry", "blocked"]
    reason: str = Field(min_length=1, max_length=2000)
    feedback: str = Field(default="", max_length=4000)
    remaining_issues: list[str] = Field(default_factory=list, max_length=12)


class AgentState(TypedDict, total=False):
    """修复图共享状态，字段覆盖更新，无自动消息聚合。

    离线回归可不配置 Checkpointer；正式 V4 使用 SQLite Checkpointer 持久化任务状态。
    Finalizer 更新 AgentState

    Attributes:
        iteration: 已进入 Coder 的次数，包含补丁被拒绝的轮次。
        index_dirty: 源码成功修改后索引同步失败，禁止最终报告完全成功。
        changes: 按文件覆盖的累计变更记录，最多八个。

AgentState 中的 status 在当前 V2 工作流里有 4 个值：
 状态             含义
 running         流程进行中。Planner 完成可执行计划后进入此状态，后续节点用它判断是否继续。
 success        流程成功结束：Reviewer 判定通过，且没有其他阻止成功的情况。
 failed         流程已执行，但未达到验收要求；例如修复重试次数用尽仍未通过。
 blocked        流程无法继续或不应继续；例如计划被阻止、节点异常、重复补丁或产物写入失败。

注意，测试结果里的 passed 是 TestResult.outcome 的取值，不是 AgentState.status。
目前 status 标注为普通 str，实际值由工作流逻辑约束，并未通过类型系统限制。

    """

    task_id: str
    user_request: str
    workspace_root: str
    repo_summary: dict
    retrieved_context: str
    plan: TaskPlan
    changes: list[ChangeRecord]
    test_result: TestResult | None
    test_history: list[dict]
    review_result: ReviewResult
    iteration: int # 当前迭代次数，包含补丁被拒绝的轮次。
    max_iterations: int
    status: str
    error: str
    index_dirty: bool # 是否成功修改后索引同步失败，禁止最终报告完全成功。
    patch_error: str
    patch_fingerprints: list[str] # 补丁指纹列表
    token_usage: dict
    final_report: dict
