"""注册统一 V1 检索、精确符号定位和显式增量同步工具。"""

from mcp_servers.context import NodeName


def register(server, ctx) -> None:
    """绑定同一工作树的 V1 服务，不复制召回、融合或重排实现。

    Args:
        server: 与文件、Git、执行工具共用的官方服务实例。
        ctx: 已校验索引归属的任务上下文。
    """
    @server.tool()
    def search_code(query: str, top_k: int = 3, node: NodeName = "runtime") -> dict[str, object]:
        """检索索引快照并保留原代码元数据；结果不代表磁盘最新版本。"""
        return ctx.invoke("search_code", node, lambda: [r.model_dump() for r in ctx.service.search_code(query, top_k)])

    @server.tool()
    def find_symbol(name: str, path: str | None = None, node: NodeName = "runtime") -> dict[str, object]:
        """从当前 Python 源码精确定位符号，使用原 AST 服务及行号协议。"""
        return ctx.invoke("find_symbol", node, lambda: [r.model_dump() for r in ctx.service.find_symbol(name, path)])

    @server.tool()
    def sync_changed_files(paths: list[str], node: NodeName = "runtime") -> dict[str, object]:
        """同步明确的业务文件变更，不允许全量缺失推断或共享其他任务索引。"""
        def sync():
            """先验证所有路径均在授权范围，再复用已有安全同步入口。"""
            if not set(paths).issubset(ctx.allowed):
                raise ValueError("同步超出任务范围")
            return ctx.service.sync_changed_files(paths)
        return ctx.invoke("sync_changed_files", node, sync)
