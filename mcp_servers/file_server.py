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
        """按 EditBatch 定点替换；重复文本须指定 occurrence，可用 start_line 校验行号。"""
        return ctx.invoke("apply_patch", node, lambda: ctx.apply(batch, allowed))

    @server.tool()
    def current_changes(node: NodeName = "runtime") -> dict[str, object]:
        """核对本任务实际残留修改并返回哈希记录，不写文件。"""
        return ctx.invoke("current_changes", node, lambda: [r.model_dump() for r in ctx.editor.current_changes()])

    @server.tool()
    def preview_patch(batch: EditBatch, allowed: list[str], node: NodeName = "runtime") -> dict[str, object]:
        """只校验并展示真实补丁，不修改源码。"""
        def preview() -> dict:
            """验证任务范围后生成预览。"""
            if not set(allowed).issubset(ctx.allowed):
                raise ValueError("预览范围越权")
            return ctx.editor.preview(batch, allowed)
        return ctx.invoke("preview_patch", node, preview)

    @server.tool()
    def add_regression_test(path: str, content: str, node: NodeName = "runtime") -> dict[str, object]:
        """仅在显式授权时创建隔离目录中的新回归测试。"""
        def create() -> dict:
            """拒绝覆盖现有测试、配置和链接。"""
            import ast
            import json
            import re
            import config
            from graph.editor import sha256
            from security.path_policy import reject_links
            if not ctx.config.allow_regression_tests or not re.fullmatch(r"tests/v5_generated/test_[a-z0-9_]+\.py", path):
                raise ValueError("回归测试路径或授权无效")
            if len(content.encode()) > ctx.workspace.max_bytes:
                raise ValueError("测试过大")
            ast.parse(content)
            target = ctx.workspace.root / path
            reject_links(target)
            manifest = ctx.artifacts / "regression_tests.json"
            registered = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
            if len(registered) >= config.MAX_REGRESSION_TEST_FILES:
                raise ValueError("新增回归测试数量达到上限")
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("x", encoding="utf-8") as stream:
                stream.write(content)
            registered[path] = sha256(target.read_bytes())
            manifest.write_text(json.dumps(registered), encoding="utf-8")
            return {"path": path, "created": True}
        return ctx.invoke("add_regression_test", node, create)
