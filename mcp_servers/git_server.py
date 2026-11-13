"""注册任务 worktree 的只读 Git 信息，不暴露提交、合并或推送。"""

from mcp_servers.context import NodeName
from workspace.manager import git


def register(server, ctx) -> None:
    """注册固定参数的 Git 查询，返回值受大小限制。

    Args:
        server: 共享官方 MCPServer。
        ctx: 已绑定 worktree 与基准提交的任务上下文。
    """
    @server.tool()
    def git_status(node: NodeName = "runtime") -> dict[str, object]:
        """返回本 worktree 的有界状态及基线，不接受任意仓库参数。"""
        return ctx.invoke("git_status", node, lambda: {
            "status": git(ctx.workspace.root, "status", "--porcelain=v1", "--untracked-files=all")[:16384],
            "workspace": str(ctx.workspace.root), "base_commit": ctx.config.base_commit})

    @server.tool()
    def git_diff(node: NodeName = "runtime") -> dict[str, object]:
        """返回用户授权文件相对固定提交的 Diff；禁止外部 diff 和 textconv。"""
        def diff():
            """拒绝超大 Diff，避免静默截断后让 Reviewer 误判。"""
            value = git(ctx.workspace.root, "diff", "--no-ext-diff", "--no-textconv",
                        ctx.config.base_commit, "--", *ctx.config.allowed_files)
            if len(value.encode("utf-8")) > 524288:
                raise ValueError("Diff 超过限额")
            return value
        return ctx.invoke("git_diff", node, diff)

    @server.tool()
    def git_log(node: NodeName = "runtime") -> dict[str, object]:
        """返回最多十条提交哈希与时间，不返回可能包含私密内容的提交正文。"""
        return ctx.invoke("git_log", node, lambda: git(ctx.workspace.root, "log", "-10",
                          "--format=%H %cI", ctx.config.base_commit))
