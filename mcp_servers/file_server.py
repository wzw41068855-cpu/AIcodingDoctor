"""注册只读文件访问和复用 V2 协议的受控编辑工具。"""

from graph.state import EditBatch
from mcp_servers.context import NodeName


def register(server, ctx) -> None:
    """向共享 MCP 服务注册文件工具，权限固定在 ctx。

    Args:
        server: 官方 MCPServer 实例。
        ctx: 单任务 ToolContext，不接受客户端重新指定工作区。
    """
    @server.tool()
    def list_files(max_files: int = 100, node: NodeName = "runtime") -> dict[str, object]:
        """返回有界文件清单；不加载任意类型或隐式同步索引。"""
        return ctx.invoke("list_files", node, lambda: ctx.service.get_repository_summary(max_files))

    @server.tool()
    def read_file(path: str, start_line: int | None = None, end_line: int | None = None,
                  node: NodeName = "runtime") -> dict[str, object]:
        """读取工作树当前源码；行号从 1 开始且首尾包含，越界与敏感文件拒绝。"""
        return ctx.invoke("read_file", node, lambda: ctx.service.get_code_context(path, start_line, end_line).model_dump())

    @server.tool()
    def snapshot(path: str, node: NodeName = "runtime") -> dict[str, object]:
        """获取已有业务文件的最新内容和必填版本哈希，不读取测试或配置作为编辑对象。"""
        return ctx.invoke("snapshot", node, lambda: ctx.editor.snapshot(path))

    @server.tool()
    def apply_patch(batch: EditBatch, allowed: list[str], node: NodeName = "runtime") -> dict[str, object]:
        """按 V2 EditBatch 执行唯一文本替换；校验权限、版本、语法，失败不盲目重试。"""
        return ctx.invoke("apply_patch", node, lambda: ctx.apply(batch, allowed))

    @server.tool()
    def current_changes(node: NodeName = "runtime") -> dict[str, object]:
        """核对本任务实际残留修改并返回哈希记录，不写文件。"""
        return ctx.invoke("current_changes", node, lambda: [r.model_dump() for r in ctx.editor.current_changes()])
