"""统一代码理解入口：索引检索与当前磁盘源码读取明确分离。

设计目标：
1. 把“索引快照检索”（search_code）与“磁盘实时读取”（get_code_context）分开，
   避免调用方误把历史索引当作实时文件内容。
2. 在单仓库边界下提供稳定 API：检索、符号定位、仓库摘要、显式增量同步。
3. 所有路径均约束在 repository_root 内，避免越界访问。
"""
import ast
import os
from pathlib import Path
from typing import Iterable

from pydantic import BaseModel

import config
from rag.loader import Loader
from rag.splitter.ast_tagger import AstTagger
from utils.operation_timing import timed


class CodeResult(BaseModel):
    """代码查询结果统一结构。

    约定：
    - 行号是 1-based 且闭区间（start_line/end_line）。
    - 不可确定的字段为 None。
    - content 仅为源码片段，不添加展示前缀（如文件名/行号标签）。
    """

    relative_path: str | None = None
    source: str | None = None
    file_type: str | None = None
    symbol_name: str | None = None
    qualified_name: str | None = None
    symbol_type: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    content: str
    score: float | None = None


class CodeSearchService:
    """代码理解服务（单仓库、单进程）。

    说明：
    - 不隐式创建向量数据库；依赖外部注入 vector_manager/retriever。
    - repository_root 是可信边界，所有文件访问和同步都在其内进行。
    - 当 retriever 与 manager 同时注入时，必须共享同一索引实例。
    """

    def __init__(self, repository_root: str | Path, *, vector_manager=None,
                 retriever=None, splitter=None, max_file_bytes: int = 1_048_576):
        self.loader = Loader(str(repository_root))
        self.root = self.loader.directory
        if type(max_file_bytes) is not int or max_file_bytes <= 0:
            raise ValueError("max_file_bytes 必须是正整数")
        self.max_file_bytes = max_file_bytes
        inherited = getattr(retriever, "vector_manager", None)
        if vector_manager is not None and inherited is not None and inherited is not vector_manager:
            raise ValueError("Retriever 与 Manager 必须共享同一索引")
        self.manager = vector_manager if vector_manager is not None else inherited
        self.retriever = retriever
        self.splitter = splitter
        self._validate_repository()

    def _validate_repository(self):
        """验证当前服务绑定仓库与索引归属一致。"""
        if self.manager is not None:
            self.manager.validate_repository(str(self.root))

    @timed("检索服务：语义检索")
    def search_code(self, query: str, top_k: int = config.HYBRID_TOP_K) -> list[CodeResult]:
        """语义检索代码（返回索引快照，不读磁盘实时内容）。

        参数:
            query: 自然语言查询。
            top_k: 最多返回数量；仍受底层 Hybrid 候选策略约束。

        返回:
            CodeResult 列表，包含检索到的 chunk 内容及其 metadata。
        """
        if not query.strip():
            raise ValueError("查询文本不能为空")
        if type(top_k) is not int or top_k <= 0:
            raise ValueError("top_k 必须是正整数")
        self._validate_repository()
        if self.retriever is None:
            if self.manager is None:
                raise RuntimeError("语义检索需要注入 Retriever 或 VectorStoreManager")
            from rag.hybrid_retriever import HybridCodeRetriever
            self.retriever = HybridCodeRetriever(vector_manager=self.manager)
        results = []
        for doc in self.retriever.invoke(query, top_k=top_k):
            metadata = doc.metadata
            path = metadata.get("relative_path") or metadata.get("source")
            if path:
                resolved = self.loader.resolve_path(path)
                source = metadata.get("source")
                if source and self.loader.resolve_path(source) != resolved:
                    raise ValueError("检索结果的 source 与 relative_path 不一致")
                relative = resolved.relative_to(self.root).as_posix()
            else:
                relative = None
            results.append(CodeResult(
                **{key: metadata.get(key) or None for key in (
                    "source", "file_type", "symbol_name", "qualified_name", "symbol_type",
                    "start_line", "end_line")},
                relative_path=relative, content=doc.page_content,
            ))
            if len(results) == top_k:
                break
        return results

    @timed("源码：读取当前磁盘上下文")
    def get_code_context(self, path: str, start_line: int | None = None,
                         end_line: int | None = None) -> CodeResult:
        """读取当前磁盘源码上下文（非索引快照）。

        行号规则：
        - 输入行号为 1-based 正整数。
        -未输入行号时返回整个文件内容。
        - 输出区间为闭区间 [start_line, end_line]。
        - 空文件在未指定范围时返回 content="" 且不强行给行号。
        """
        doc = self.loader.read_file(path, self.max_file_bytes)
        lines = doc.page_content.split("\n")
        if lines[-1] == "":
            lines.pop()
        count = len(lines)
        for value in (start_line, end_line):
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError("行号必须为正整数")
        if not count and start_line is None and end_line is None:
            return CodeResult(**doc.metadata, content="")
        first = 1 if start_line is None else start_line
        last = count if end_line is None else end_line
        if not 1 <= first <= last <= count:
            raise ValueError(f"非法行范围；文件共 {count} 行")
        # 保留所选行原有的换行结尾。
        physical = doc.page_content.split("\n")
        kept = [line + ("\n" if i < len(physical) - 1 else "") for i, line in enumerate(physical)][:count]
        return CodeResult(**{**doc.metadata, "start_line": first, "end_line": last},
                          content="".join(kept[first - 1:last]))

    def _files(self):
        """按仓库规则枚举可处理文件（已过滤隐藏目录/忽略目录/符号链接目录）。

        生成器函数，作用是：按仓库规则遍历目录，产出“允许处理”的文件相对路径。
        它不是一次性返回列表，而是 yield 一个个给调用方，便于上层按需消费
        （例如 get_repository_summary() 逐个收集直到上限）。
        """

        def on_error(error):
            raise error

        for directory, dirs, files in os.walk(self.root, followlinks=False,
                                              onerror=on_error):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".")
                             and d.lower() not in self.loader.IGNORED_DIRS
                             and not (Path(directory) / d).is_symlink())
            for name in sorted(files):
                path = Path(directory) / name
                if path.suffix.lower() not in Loader.SUPPORTED_SUFFIXES:
                    continue
                try:
                    self.loader.resolve_path(str(path))
                except ValueError:
                    continue
                yield path.relative_to(self.root).as_posix()

    @timed("源码：AST 符号定位")
    def find_symbol(self, name: str, path: str | None = None) -> list[CodeResult]:
        """通过ast，按符号名从源码精确定位源码片段（支持短名与限定名）。

        匹配策略：
        - `name` 同时匹配 `symbol.name`（短名）与 `symbol.qualified_name`（限定名）。
        - 仅处理 Python 文件（`.py`）。
        - 若定义存在装饰器，返回片段起始行提升到最早装饰器行。

        Args:
            name: 待查找的符号名，不能为空字符串。
            path: 可选；仅在指定相对路径文件中查找。
                  为 None 时，会在仓库规则允许的全部文件中遍历（通过 `_files()`）。

        Returns:
            `CodeResult` 列表。每个元素包含：
            - 命中的符号元数据（symbol_name/qualified_name/symbol_type）
            - 精确行号区间（start_line/end_line，1-based，闭区间）
            - 对应源码片段（`content`）

        Raises:
            ValueError: 当 `name` 为空白时抛出。
            ValueError: 当 `path` 越界或非法（由 `loader.resolve_path` 校验）时抛出。
            SyntaxError: 当目标文件 Python 语法非法导致 `ast.parse` 失败时抛出。

        eg：
            service.find_symbol("my_function")  # 查找所有文件中名为 my_function 的符号
            返回值eg：
            [
                CodeResult(
                    relative_path="src/module.py",
                    source="src/module.py",
                    file_type=".py",
                    symbol_name="my_function",
                    qualified_name="module.my_function",
                    symbol_type="function",
                    start_line=10,
                    end_line=20,
                    content="def my_function():\n    pass\n"
                ),
                ...
            ]
        """
        # 防御式校验：禁止空符号名，避免无意义全量扫描。
        if not name.strip():
            raise ValueError("符号名不能为空")
        paths = [path] if path is not None else self._files()
        results = []
        for file in paths:
            resolved = self.loader.resolve_path(file)
            if resolved.suffix.lower() != ".py":
                continue
            # 读取实时磁盘内容（非索引快照）。
            context = self.get_code_context(file)
            tree = ast.parse(context.content)
            starts = {node.lineno: min([node.lineno, *(d.lineno for d in node.decorator_list)])
                      for node in ast.walk(tree)
                      if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
            for symbol in AstTagger.collect_symbols(context.content):
                if name not in (symbol.name, symbol.qualified_name):
                    continue
                first = starts[symbol.start_line]
                lines = context.content.split("\n")
                content = "\n".join(lines[first - 1:symbol.end_line])
                if symbol.end_line < len(lines):
                    content += "\n"
                results.append(context.model_copy(update={
                    "symbol_name": symbol.name, "qualified_name": symbol.qualified_name,
                    "symbol_type": symbol.symbol_type, "start_line": first,
                    "end_line": symbol.end_line, "content": content,
                }))
        return results

    @timed("仓库：扫描文件清单")
    def get_repository_summary(self, max_files: int = 100) -> dict:
        """返回仓库文件清单摘要（有界）。

        返回:
            {"files": [...], "truncated": bool}
            - truncated=True 表示仍有更多文件未列出。

            eg：
            {
                "files": [
                    "src/module1.py",
                    "src/module2.py",
                    "docs/readme.md",
                    ...
                ],
                "truncated": False
            }
        """
        if type(max_files) is not int or not 1 <= max_files <= 1000:
            raise ValueError("max_files 必须在 1 到 1000 之间")
        files = []
        for path in self._files():
            if len(files) == max_files:
                return {"files": files, "truncated": True}
            files.append(path)
        return {"files": files, "truncated": False}

    @timed("索引：增量同步文件")
    def sync_changed_files(self, paths: Iterable[str]) -> dict[str, int]:
        """按显式变更集执行增量同步（全成全败）。

        语义：
        - 输入路径中存在文件 => 作为“更新/新增”候选。
        - 输入路径中不存在文件 => 作为“删除”候选。
        - 任一待更新文件读取失败 => 整批取消，保持旧索引不变。
        """
        if self.manager is None:
            raise RuntimeError("同步需要注入 VectorStoreManager")
        if isinstance(paths, (str, bytes)):
            raise ValueError("paths 必须是文件路径列表")
        self._validate_repository()
        existing, deleted = [], []
        for path in dict.fromkeys(paths):
            resolved = self.loader.resolve_path(path)
            relative = resolved.relative_to(self.root).as_posix()
            try:
                resolved.stat()
            except FileNotFoundError:
                deleted.append(relative)
            else:
                existing.append(relative)
        documents = self.loader.load_files(existing, self.max_file_bytes)
        if self.loader.failed_files:
            raise RuntimeError("部分文件加载失败，取消整批同步；旧索引保持不变")
        if not documents and not deleted:
            return {"added": 0, "updated": 0, "skipped": 0, "deleted": 0}
        if self.splitter is None:
            from rag.splitter.code_splitter import CodeSplitter
            self.splitter = CodeSplitter()
        return self.manager.sync_documents(
            documents, self.splitter, remove_missing=False,
            deleted_paths=deleted, repository_root=str(self.root),
        )
