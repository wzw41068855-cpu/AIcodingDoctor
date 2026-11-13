"""只提供启动时固定的 Docker pytest 操作，不注册 Shell 或 Ruff。"""

from mcp_servers.context import NodeName
import anyio



def register(server, ctx) -> None:
    """注册仅用于运行固定 pytest 的 MCP 工具。

    设计意图：
        - 不暴露任意命令执行入口（无 Shell / Ruff / 自定义参数）。
        - 执行范围在启动时即被固定：镜像、测试白名单、超时都来自 `ctx`。
        - 每次执行都走 `ctx.invoke(...)`，统一审计、错误格式和阻塞语义。

    Args:
        server: 共享的 MCPServer 实例，用于注册工具函数。
        ctx: 任务上下文，持有 DockerRunner、审计器与权限边界。
    """

    @server.tool()
    async def run_pytest(node: NodeName = "runtime") -> dict[str, object]:
        """在线程中运行固定容器测试，并在取消时等待清理完成后再退出。

        为什么这样实现：
            - Docker CLI 是同步阻塞调用，不能直接阻塞事件循环。
            - MCP 工具是异步接口，需通过工作线程桥接阻塞代码。
            - 收到取消时，先通知 runner 停止，再等待收尾，避免遗留容器。

        Args:
            node: 审计归因节点名（仅用于日志归因，不参与授权决策）。

        Returns:
            统一结构化结果，兼容 V2 TestResult 的字典表示：
            成功时通常为 {"ok": True, "data": ...}
            失败时通常为 {"ok": False, "error_type": ..., "blocked": ..., "log_path": ...}

        Notes:
            - `done` 用于通知外层“执行路径已结束（成功/失败/取消收尾）”。
            - `results` 存放唯一一次调用返回值，最终返回 `results[0]`。
        """
        done, results = anyio.Event(), []

        async def execute(*, task_status=anyio.TASK_STATUS_IGNORED):
            """执行受保护的测试任务，并保证总能发出完成信号。

            关键保证：
                1) 先 `task_status.started()` 完成 task group 启动握手；
                2) 使用 `CancelScope(shield=True)` 保护启动与收尾关键区；
                3) 无论成功/异常/取消，`finally` 都会 `done.set()`，防止外层永久等待。

            说明：
                - 屏蔽取消并不意味着忽略取消请求；
                - 真实停止仍通过 `ctx.runner.cancel` 触发有界收尾。
            """
            with anyio.CancelScope(shield=True):#当前作用域内的操作，不受外层取消作用域发出的取消请求影响。
                # 告知 `group.start(execute)`：任务已进入可管理状态
                task_status.started()
                try:
                    # 在工作线程执行阻塞 Docker 调用，避免阻塞事件循环
                    results.append(
                        await anyio.to_thread.run_sync(
                            lambda: ctx.invoke(
                                "run_pytest",
                                node,
                                lambda: ctx.runner.run().model_dump(),
                            )
                        )
                    )
                finally:
                    # 保证外层 wait 必有唤醒点
                    done.set()

        async with anyio.create_task_group() as group:
            # 启动握手阶段也受保护，避免“半启动 + 未设 done”造成死等
            with anyio.CancelScope(shield=True):
                await group.start(execute)

            try:
                # 正常路径：等待执行结束
                await done.wait()
            except anyio.get_cancelled_exc_class():
                # 取消路径：先请求 runner 停止，再等待收尾完成
                cancel = getattr(ctx.runner, "cancel", None)
                if cancel is not None:
                    cancel.set()

                # 收尾等待不应再被取消打断
                with anyio.CancelScope(shield=True):
                    await done.wait()
                # 保持取消语义向上传播
                raise

        return results[0]
