"""绑定任务权限和现有业务对象，工具参数不能扩大工作区或执行范围。"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from audit.writer import AuditError, AuditWriter
from graph.editor import Editor, Workspace
from graph.state import EditBatch
from sandbox.docker_runner import DockerRunner
from workspace.manager import git


NodeName = Literal["runtime", "Planner", "Coder", "Tester", "Reviewer", "Finalizer"]


class TaskConfig(BaseModel):
    """宿主 CLI 写入的固定任务配置，不属于可由模型修改的工具输入。

    这个模型的职责不是“接收用户需求”，而是“接收宿主 CLI 已验证过的固定配置”。
    它的目标是把任务边界固定死：工作区、产物目录、允许修改文件、测试路径、基线 commit、超时与镜像都必须在允许运行的白名单内。

    字段说明:
        task_id: 本次任务的唯一标识，必须是 32 位十六进制字符串。
        workspace: 可信工作区路径，必须是已经授权的 Git worktree。
        artifacts: 任务产物目录，存放审计日志、索引、报告和中间数据，不允许与工作区混用。
        base_commit: 固定基线提交 SHA，工具必须在该提交之上执行或审查。
        allowed_files: 用户明确授权允许修改的业务文件白名单，最多 8 个文件。
        tests: 允许执行的固定 pytest 路径白名单，最多 8 个测试目标。
        image: 集成测试使用的 Docker 镜像名称。
        timeout: pytest 最大执行时长，必须在 (0, 300] 秒内。
        trusted_copy: 必须显式为 True，表示该副本已被声明可信。
        mock_index: 是否使用离线模拟索引，默认 False。

    安全约束:
        - 不允许额外字段，避免配置被扩展污染。
        - 不允许空白名单、不允许越界路径、不允许非 Git 基线。
        - 所有字段都应由宿主 CLI 先校验后写入，不能由模型动态覆盖。
    """

    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    workspace: str
    artifacts: str
    base_commit: str = Field(pattern=r"^[a-f0-9]{40,64}$")
    allowed_files: list[str] = Field(min_length=1, max_length=8)
    tests: list[str] = Field(min_length=1, max_length=8)
    image: str
    timeout: float = Field(default=60, gt=0, le=300)
    trusted_copy: Literal[True]
    mock_index: bool = False


class ToolContext:
    """共享文件快照、索引、DockerRunner 和单写入审计的任务上下文。

    它的核心思想是：
    任务一启动，就把“工作区 + 允许访问的文件 + 允许运行的测试 + 固定基线提交”锁死。
    之后 MCP 工具调用不再接收任意新路径或新范围，只能在这个已经审计过的任务边界内工作。
    任何异常都会走统一的审计记录，而不是直接暴露原始异常正文。

    Attributes:
        config: 启动时冻结使用的任务配置，不接受工具调用覆盖。
        audit: 一旦审计故障，拒绝后续工具副作用。
        editor: V2 Editor，保留唯一替换、必填哈希及批量验证语义。
    """

    def __init__(self, config: TaskConfig, *, service=None, runner=None):
        """验证 worktree 归属并注入任务级依赖，不在宿主执行项目源码。

        Args:
            config: CLI 生成且不包含密钥的配置。
            service: 测试用检索服务替身；生产默认初始化真实 V1 服务。
            runner: 仅供单元测试依赖注入；stdio 入口始终创建 DockerRunner。
        Raises:
            ValueError: 目录、Git 基线、白名单或服务归属不匹配。
            AuditError: 初始化审计失败；后续操作保持阻塞。
        """
        self.config = config
        self.artifacts = Path(config.artifacts).resolve(strict=True)
        #artifacts 是任务产物目录，通常放：
        # 审计日志 audit.jsonl
        # 索引目录
        # 报告、临时文件、pytest 日志等
        self.audit = AuditWriter(self.artifacts / "audit.jsonl", config.task_id)
        with self.audit.operation("task.initialize"):
            self.workspace = Workspace(config.workspace, trusted_copy=config.trusted_copy)
            marker = self.workspace.root / ".git"
            if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 4096:
                raise ValueError("任务必须使用普通 Git worktree 标记文件")
            self._git_marker = marker.read_bytes()
            if (self.artifacts.is_relative_to(self.workspace.root)
                    or self.workspace.root.parent != self.artifacts
                    or git(self.workspace.root, "rev-parse", "HEAD").strip() != config.base_commit):
                raise ValueError("任务路径或基线不匹配")
            self.allowed = frozenset(config.allowed_files)
            for path in self.allowed:
                self.workspace.path(path)
            self.editor = Editor(self.workspace)
            # V4 每个审批等待点会关闭 MCP 会话；从固定 Git 提交恢复编辑基线，
            # 防止新会话丢失净修改记录。git show 仅访问已校验的白名单源码。
            for path in self.allowed:
                baseline = git(self.workspace.root, "show", config.base_commit + ":" + path)
                current = self.workspace.path(path).read_bytes()
                # Git 工具返回 UTF-8 文本；非 UTF-8 源码明确阻塞，不猜测编码。
                if baseline.encode("utf-8") != current:
                    self.editor.originals[path] = baseline.encode("utf-8")
            self.runner = runner if runner is not None else DockerRunner(
                self.workspace, config.tests, config.image, timeout=config.timeout,
                task_id=config.task_id, artifacts=self.artifacts)
            if service is None:
                if config.mock_index:
                    from graph.demo import demo_service
                    service = demo_service(self.workspace.root, self.artifacts)
                else:
                    from rag.vector_store import VectorStoreManager
                    from services.code_search import CodeSearchService
                    index = self.artifacts / "index"
                    manager = VectorStoreManager(str(index), "v3_" + config.task_id)
                    service = CodeSearchService(self.workspace.root, vector_manager=manager, max_file_bytes=65536)
                    summary = service.get_repository_summary(max_files=100)
                    if summary["truncated"]:
                        raise ValueError("V3 首期仓库最多 100 个核心索引文件")
                    service.sync_changed_files(summary["files"])
            if Path(service.root).resolve() != self.workspace.root:
                raise ValueError("索引和工作树不匹配")
            self.service = service

    def invoke(self, tool: str, node: NodeName, function) -> dict:
        """统一工具执行入口。
        在单任务审计锁内执行确定性工具，返回统一的结构化结果。
        这个方法把“工具调用 + 审计 + 安全校验 + 统一返回结构”整合在一起了，避免各个工具自己处理不一致。

        Args:
            tool: 固定注册的工具名。
            node: 调用方节点名，仅用于日志归因，不参与授权。
            function: 内部绑定的零参数业务操作，不接受远程代码。
        Returns:
            ok/data 或 ok/error_type/blocked/log_path；不携带异常正文。
        """
        try:
            with self.audit.operation(tool, node) as event:
                if (self.workspace.root.is_symlink() or self.workspace.root.is_junction()
                        or self.workspace.root.resolve() != self.workspace.root
                        or (self.workspace.root / ".git").read_bytes() != self._git_marker):
                    raise ValueError("任务工作树被替换")
                data = function()
                if tool == "apply_patch":
                    event["changes"] = data
                if tool == "run_pytest":
                    event.update(exit_code=data["returncode"], outcome=data["outcome"], log_path=data["log_path"])
            return {"ok": True, "data": data}
        except Exception as exc:
            return {"ok": False, "error_type": type(exc).__name__,
                    "blocked": isinstance(exc, AuditError) or self.audit.blocked,
                    "log_path": str(self.audit.path)}

    def apply(self, batch: EditBatch, allowed: list[str]) -> list[dict]:
        """把计划范围与用户授权取交集，沿用原 Editor 批量替换协议。

        Args:
            batch: 复用 V2 EditBatch，expected_hash 必填。
            allowed: Planner 校验后的本轮目标，不得扩大用户白名单。
        Returns:
            现有 ChangeRecord 的 JSON 表示，仅含路径及前后哈希。
        Raises:
            ValueError: 目标超出用户授权或补丁被原编辑器拒绝。
        """
        if not allowed or not set(allowed).issubset(self.allowed):
            raise ValueError("计划超出用户授权的文件范围")
        return [r.model_dump() for r in self.editor.apply(batch, allowed)]
