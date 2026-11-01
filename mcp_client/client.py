"""让同步 Graph 通过持久 stdio MCP 会话调用工具，不重写节点。"""

from contextlib import contextmanager, ExitStack
import os
from pathlib import Path
import sys

from anyio.from_thread import start_blocking_portal
from mcp import Client
from mcp.client.stdio import StdioServerParameters

import config
from audit.writer import AuditError
from graph.state import ChangeRecord, TestResult
from services.code_search import CodeResult


class ToolRPC:
    """在一个事件循环线程中复用官方异步客户端。

    Attributes:
        node: 当前 Graph 节点名，用于跨进程审计归因。
        blocked: 无法确认写操作结果或审计失效时锁死后续调用。
    """

    def __init__(self, portal, client, timeout: float):
        """绑定已经打开的客户端，不自行启动线程或业务操作。

        Args:
            portal: AnyIO BlockingPortal，生命周期由 connect 管理。
            client: 同一循环中进入异步上下文的官方 MCP Client。
            timeout: 单次 RPC 等待秒数，不表示重试次数。
        """
        self.portal, self.client, self.timeout = portal, client, timeout
        self.node, self.blocked = "runtime", False

    def call(self, tool: str, **arguments):
        """发起一次工具请求，不自动重试可能已经发生副作用的调用。

        Args:
            tool: 注册的工具名。
            **arguments: 工具参数；客户端统一附加节点名称。
        Returns:
            工具业务 data，保持现有业务协议。
        Raises:
            AuditError: 审计失效或远程结果无法确认，必须保留现场。
            ValueError: 远程参数或补丁校验拒绝。
            RuntimeError: 工具依赖异常或协议错误。
        """
        if self.blocked:
            raise AuditError("远程任务已阻塞，禁止自动重试")

        async def invoke():
            """在 MCP 所属循环等待请求；SDK 负责取消协议，宿主不嵌套 asyncio.run。"""
            return await self.client.call_tool(tool, dict(arguments, node=self.node),
                                               read_timeout_seconds=self.timeout)
        try:
            result = self.portal.call(invoke)
        except BaseException as exc:
            self.blocked = True
            raise AuditError("MCP 结果未确认，保留工作树和审计") from exc
        data = result.structured_content
        if result.is_error or not isinstance(data, dict) or "ok" not in data:
            self.blocked = True
            raise AuditError("MCP 请求被拒绝或结果未确认，保留现场")
        if not data["ok"]:
            if data.get("blocked"):
                self.blocked = True
                raise AuditError("服务端审计失效，保留现场")
            error = data.get("error_type")
            if error in {"ValueError", "SyntaxError", "UnicodeError"}:
                raise ValueError("远程工具拒绝参数、权限或补丁")
            raise RuntimeError("远程工具失败：" + str(error))
        return data["data"]


@contextmanager
def connect(config_path: Path, timeout: float = 360):
    """启动唯一 stdio 服务，并将其生命周期绑定到同步调用作用域。

    Args:
        config_path: CLI 生成的任务配置，不含模型密钥。
        timeout: RPC 最大等待时间；超时不盲目重试文件修改。
    Yields:
        ToolRPC；上下文退出时关闭会话、服务进程和循环线程。
    Notes:
        子进程只接收必要系统环境和明确的百炼凭据；容器永不继承该凭据。
        事件循环及 MCP 客户端始终在同一线程，避免取消作用域跨任务错误。
    """
    #构造 MCP 服务环境：只把白名单里的环境变量传给服务子进程，再添加 UTF-8 设置，避免无关环境变量（包括其他密钥）被一并继承。
    names = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "USERPROFILE", "HOME",
             "CONDA_PREFIX", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DASHSCOPE_API_KEY"}
    env = {k: v for k, v in os.environ.items() if k.upper() in names}
    env["PYTHONIOENCODING"] = "utf-8"
    #配置 stdio 服务：
    parameters = StdioServerParameters(command=sys.executable,
        args=["-m", "mcp_servers.server", "--config", str(config_path)],
        env=env, cwd=str(config.BASE_DIR))

    with ExitStack() as stack:
        #start_blocking_portal() 创建一个后台线程来运行 AnyIO 事件循环。同步代码可以通过 portal 调用异步操作。
        portal = stack.enter_context(start_blocking_portal())
        client = stack.enter_context(portal.wrap_async_context_manager(
            Client(parameters, read_timeout_seconds=timeout)))
        yield ToolRPC(portal, client, timeout)


class SearchProxy:
    """保留 V1 服务的节点调用面，所有内容和索引操作经 MCP。

    Attributes:
        root: 与工作区相同的固定根目录。
        retriever: 非 None 标志，使原 Planner 使用远程语义检索。
        manager: None；索引对象仅存在于服务端，不跨进程共享。
    """

    def __init__(self, root: Path, rpc: ToolRPC):
        """绑定根目录及 RPC，不创建本地向量库或读取业务文件。"""
        self.root, self.rpc = root, rpc
        self.retriever, self.manager = True, None

    def get_repository_summary(self, max_files=100):
        """经 list_files 获取有界清单，返回原 files/truncated 字典。"""
        return self.rpc.call("list_files", max_files=max_files)

    def get_code_context(self, path, start_line=None, end_line=None):
        """读取远程磁盘当前源码，将原行号和元数据还原为 CodeResult。"""
        return CodeResult.model_validate(self.rpc.call("read_file", path=path, start_line=start_line, end_line=end_line))

    def search_code(self, query, top_k=3):
        """通过原检索协议获取最多 top_k 个索引快照，不伪造相关性分数。"""
        return [CodeResult.model_validate(r) for r in self.rpc.call("search_code", query=query, top_k=top_k)]

    def find_symbol(self, name, path=None):
        """通过 AST 定位符号并还原结果，保持 V1 精确位置语义。"""
        return [CodeResult.model_validate(r) for r in self.rpc.call("find_symbol", name=name, path=path)]

    def sync_changed_files(self, paths):
        """显式同步本任务变更列表，返回原新增、更新、跳过和删除统计。"""
        return self.rpc.call("sync_changed_files", paths=paths)


class EditorProxy:
    """保留原 Editor 接口；版本检查和文件写入只发生在 MCP 服务端。"""

    def __init__(self, workspace, rpc: ToolRPC):
        """绑定用于本地路径权限预检的 Workspace 与远程工具会话。"""
        self.workspace, self.rpc = workspace, rpc

    def snapshot(self, path):
        """获取业务文件当前文本和 expected_hash，不缓存旧轮次源码。"""
        return self.rpc.call("snapshot", path=path)

    def apply(self, batch, allowed):
        """发送现有 EditBatch 和计划范围，返回兼容的 ChangeRecord 列表。"""
        return [ChangeRecord.model_validate(r) for r in self.rpc.call(
            "apply_patch", batch=batch.model_dump(), allowed=allowed)]

    def current_changes(self):
        """查询服务端核对后的实际修改记录，不信任仅存在于 Graph 的变更声明。"""
        return [ChangeRecord.model_validate(r) for r in self.rpc.call("current_changes")]

    def diff(self):
        """通过 Git 返回相对固定提交的实际 Diff，用于原 Reviewer 和 Finalizer。"""
        return self.rpc.call("git_diff")


class RunnerProxy:
    """保留 Tester 依赖的 tests/run 接口，执行目标不能由模型修改。"""

    def __init__(self, workspace, tests: list[str], rpc: ToolRPC):
        """绑定固定测试集及工作区，供 RepairWorkflow 校验依赖归属。"""
        self.workspace, self.tests, self.rpc = workspace, list(tests), rpc

    def run(self):
        """调用无执行参数的远程 pytest 工具，还原原 TestResult 分类。"""
        return TestResult.model_validate(self.rpc.call("run_pytest"))
