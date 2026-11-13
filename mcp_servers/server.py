"""一个 stdio MCP 服务进程承载四类工具，stdout 只用于协议。"""

import argparse
import logging
from pathlib import Path
import sys
from time import perf_counter
from uuid import uuid4

import anyio

from mcp.server import MCPServer

from mcp_servers.context import TaskConfig, ToolContext
from mcp_servers import code_search_server, execution_server, file_server, git_server


def create_server(context: ToolContext) -> MCPServer:
    """将已绑定依赖注册到一个官方服务，便于真实协议与依赖替身测试。
    创建官方 MCPServer 实例；
        注册四个工具模块；
        挂一个请求级审计中间件。
    功能：
        1. 文件操作：列出、读取、快照、应用补丁、获取当前变更。
        2. 代码搜索：搜索代码、查找符号、同步变更文件。
        3. Git 操作：状态、差异、日志。
        4. 执行器：运行 pytest。

    Args:
        context: 同一任务的文件、搜索、执行和审计状态。
    Returns:
        仅含四类固定工具的 MCPServer，不启动 HTTP 服务。
    """
    server = MCPServer("AICodingDoctor V3", log_level="CRITICAL")
    for module in (file_server, code_search_server, git_server, execution_server):
        module.register(server, context)#它把文件、搜索、Git、执行四类能力统一挂到同一个 MCP 进程里。
    lock = anyio.Lock()#anyio.Lock 是异步锁，专门给 async/await 用：
    known = {"list_files", "read_file", "snapshot", "apply_patch", "current_changes",
             "search_code", "find_symbol", "sync_changed_files", "git_status", "git_diff",
             "git_log", "run_pytest"}

    async def audit_request(request, call_next):
        """审计 SDK 参数校验前的请求，串行执行避免跨工具状态竞争。

        为每个工具调用包一层统一审计壳，确保无论成功、失败还是异常，都有成对日志。

        SDK 在调用业务函数前也会拒绝非法参数，因此只在业务函数中记录
        无法覆盖所有拒绝。此处不持有线程锁跨 await，避免同步处理器死锁。
        异步锁仅管理本服务请求，不实现跨进程任务调度。
        """
        if request.method != "tools/call":
            return await call_next(request)#只对工具调用做审计。其他协议请求（比如初始化、能力协商）不进入这套“工具事务”统计，避免噪音。
        async with lock:#它把一个 tools/call 的完整生命周期（start -> 执行 -> end）包成临界区。
            params = request.params if isinstance(request.params, dict) else {}
            tool = params.get("name")
            tool = tool if isinstance(tool, str) and tool in known else "unknown_tool"
            call_id, started = uuid4().hex, perf_counter()
            context.audit.append({"phase": "start", "tool": "rpc." + tool, "call_id": call_id})
            status = "completed"
            try:
                result = await call_next(request)
                #request：当前这次 MCP 请求对象
                #call_next：表示“按正常链路继续处理这个请求”
                #await call_next(request)：等待真正的工具处理函数执行完，并拿回结果
                if (getattr(result, "is_error", False)
                        or isinstance(result, dict) and (result.get("isError") or result.get("is_error"))):
                    status = "rejected"
                return result
            except BaseException:
                status = "rejected"
                raise
            finally:
                context.audit.append({"phase": "end", "tool": "rpc." + tool, "call_id": call_id,
                                      "status": status, "duration_seconds": perf_counter() - started})
    server.middleware.append(audit_request)
    #把刚才定义的 audit_request 插入到 MCPServer 的“中间件列表”里。
    #“以后每一次请求进来，先经过这个中间件，再进入真正的工具处理逻辑。”
    return server


def main() -> int:
    """读取 CLI 生成的非敏感配置并启动 stdio 服务。

    Returns:
        正常退出 0，配置或初始化阻塞 2；错误仅输出类型至 stderr。
    Notes:
        业务依赖不修改协议 stdout。任务退出后保留 worktree、索引及审计，
        不自动删除用户需要审查的修复产物。
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    # 禁用可能含 SDK 异常正文的旧日志，只单独启用固定标签的性能日志。
    logging.getLogger("CodingDoctor2").setLevel(logging.CRITICAL)
    logging.getLogger("mcp").setLevel(logging.CRITICAL)
    timer = logging.getLogger("CodingDoctor2.timing")
    timer.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
    timer.addHandler(handler)
    timer.propagate = False
    try:
        config = TaskConfig.model_validate_json(Path(args.config).read_text(encoding="utf-8"))
        create_server(ToolContext(config)).run(transport="stdio")
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        print("V3 MCP 阻塞：" + type(exc).__name__, file=sys.stderr)
        return 2
    finally:
        timer.removeHandler(handler)
        handler.close()


if __name__ == "__main__":
    raise SystemExit(main())
