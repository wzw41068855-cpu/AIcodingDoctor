"""AST 父上下文、实时版本和静态调用位置的轻量适配。"""

import ast
import hashlib
from typing import Any

import config


def source_version(content: str) -> str:
    """计算规范化源码版本。

    Args:
        content: Loader 已规范化换行的全文。
    Returns:
        SHA256。
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def parent_metadata(content: str, first: int, last: int, path: str) -> dict:
    """找到包含子区间的最外层类或函数，不重新实现切分器。

    Args:
        content: 当前源码全文。
        first: 子块起始行。
        last: 子块结束行。
        path: 仓库相对路径。
    Returns:
        父区间及源码版本。
    """
    start, end, symbol = first, last, ""
    try:
        tree = ast.parse(content)
        enclosing = [n for n in ast.walk(tree) if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                     and min([n.lineno, *[d.lineno for d in n.decorator_list]]) <= first
                     and n.end_lineno >= last]
        if enclosing:
            node = max(enclosing, key=lambda n: n.end_lineno - n.lineno)
            start = min([node.lineno, *[d.lineno for d in node.decorator_list]])
            end, symbol = node.end_lineno, node.name
    except SyntaxError:
        pass
    return {"parent_id": source_version(f"{path}:{symbol}:{start}:{end}"),
            "parent_start_line": start, "parent_end_line": end,
            "parent_symbol": symbol, "source_version": source_version(content)}


class CodeContextMixin:
    """复用服务已有 Loader、检索和读文件方法。"""

    def search_context(self, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        """子块召回后按有界预算扩展当前源码。

        Args:
            query: 检索问题。
            top_k: 召回上限。
        Returns:
            子证据、当前父源码、版本和截断标识。
        """
        if type(top_k) is not int or not 1 <= top_k <= config.RETRIEVAL_MAX_RESULTS:
            raise ValueError("检索数量超限")
        rows, seen, remaining = [], set(), config.RETRIEVAL_MAX_CHARS
        for child in self.search_code(query, top_k):
            path = child.relative_path
            if not path:
                continue
            try:
                current = self.get_code_context(path)
            except FileNotFoundError:
                rows.append({"child": child.model_dump(), "deleted": True, "stale": True, "parent": None})
                continue
            lines = current.content.splitlines(True)
            if not lines:
                continue
            first, last = child.start_line or 1, child.end_line or 1
            stale = child.source_version != current.source_version
            if stale and child.symbol_name:
                matches = self.find_symbol(child.qualified_name or child.symbol_name, path)
                if not matches:
                    rows.append({"child": child.model_dump(), "stale": True, "parent": None})
                    continue
                first, last = matches[0].start_line, matches[0].end_line
            first = min(first, len(lines))
            last = min(max(first, last), len(lines))
            metadata = parent_metadata(current.content, first, last, path)
            start, end = metadata["parent_start_line"], metadata["parent_end_line"]
            start = max(start, first - config.RETRIEVAL_PARENT_LINES // 2)
            end = min(end, start + config.RETRIEVAL_PARENT_LINES - 1)
            limit = min(remaining, config.RETRIEVAL_PARENT_CHARS)
            # 按完整行截断，确保返回内容与标注行号严格一致。
            kept = []
            for line in lines[start - 1:end]:
                if len("".join(kept)) + len(line) > limit:
                    break
                kept.append(line)
            if not kept:
                continue
            actual_end = start + len(kept) - 1
            key = (path, start, actual_end)
            if key in seen:
                continue
            seen.add(key)
            content = "".join(kept)
            remaining -= len(content)
            parent = current.model_copy(update={"content": content, "start_line": start, "end_line": actual_end})
            rows.append({"child": child.model_dump(), "parent": parent.model_dump(), "stale": stale,
                         "truncated": actual_end < metadata["parent_end_line"] or start > metadata["parent_start_line"], **metadata})
        return rows

    def find_references(self, name: str, path: str | None = None) -> list:
        """检索静态调用位置；动态分派仅返回可证明的名字匹配。

        Args:
            name: 函数短名或限定名。
            path: 可选的单文件范围。
        Returns:
            带真实文件与行号的调用片段。
        """
        if not name.strip():
            raise ValueError("符号不能为空")
        rows = []
        for file in [path] if path else self._files():
            if not file.endswith(".py"):
                continue
            current = self.get_code_context(file)
            try:
                tree = ast.parse(current.content)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                called = ast.unparse(node.func)
                if called != name and called.split(".")[-1] != name.split(".")[-1]:
                    continue
                rows.append(self.get_code_context(file, node.lineno, node.end_lineno))
                if len(rows) >= config.REFERENCE_MAX_RESULTS:
                    return rows
        return rows
