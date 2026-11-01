"""使用 Python 标准库 AST，为代码 Chunk 补充可检索的结构 metadata。
AstTagger 不负责切分代码，而是通过 AST 分析 Python 源码，
给已经生成的 Chunk 添加函数名、类名、行号关联等 metadata。
"""
import ast
import json
from dataclasses import dataclass

from langchain_core.documents import Document
from utils.logger import get_logger

logger = get_logger("ast_tagger")


@dataclass(frozen=True)
class CodeSymbol:
    """一条静态代码定义；用不可变对象避免遍历过程意外修改。
    eg
    "module_path": "services.user",
    "symbol_name": "login",
    "symbol_type": "method",
    "parent_class": "UserService",
    "qualified_name": "UserService.login",
    "symbol_names": '["UserService", "UserService.login"]',
    """

    name: str
    qualified_name: str#完整路径名，用于唯一定位语义位置。例如 MyClass.run 或 pkg.mod.func 这种层级标识
    symbol_type: str#symbol_name 是短名，比如函数名 foo、方法名 bar；
    start_line: int
    end_line: int
    parent_class: str = ""#parent_class 则专门记录所属类上下文，便于区分“同名函数但不同类”的情况


class _SymbolVisitor(ast.NodeVisitor):
    """记录语法定义的层级，而非靠字符串猜测函数所属的类。
    _SymbolVisitor 只负责收集符号信息。
    """

    def __init__(self):
        self.symbols: list[CodeSymbol] = []
        self.names: list[str] = []
        # 栈内存放 (类型, 名称)，可区分“类里的函数”和“函数里的函数”
        self.scopes: list[tuple[str, str]] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qualified_name = ".".join([*self.names, node.name])
        enclosing_classes = [name for kind, name in self.scopes if kind == "class"]
        self.symbols.append(CodeSymbol(
            name=node.name, qualified_name=qualified_name,
            symbol_type="class", start_line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            parent_class=".".join(enclosing_classes),
        ))
        self.names.append(node.name)
        self.scopes.append(("class", node.name))
        self.generic_visit(node)  # 继续访问类内部方法
        self.scopes.pop()
        self.names.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        parent_is_class = bool(self.scopes) and self.scopes[-1][0] == "class"
        symbol_type = (
            "method" if parent_is_class else
            "nested_function" if self.scopes else "function"
        )
        enclosing_classes = [name for kind, name in self.scopes if kind == "class"]
        self.symbols.append(CodeSymbol(
            name=node.name,
            qualified_name=".".join([*self.names, node.name]),
            symbol_type=symbol_type,
            start_line=node.lineno, end_line=node.end_lineno or node.lineno,
            parent_class=".".join(enclosing_classes),
        ))
        self.names.append(node.name)
        self.scopes.append(("function", node.name))
        self.generic_visit(node)  # 包括嵌套函数
        self.scopes.pop()
        self.names.pop()

    visit_AsyncFunctionDef = visit_FunctionDef


class AstTagger:
    """给已有 Chunk 添加结构标签，不改源码、切分结果或已有行号。
    AstTagger：真正向 Document 添加 metadata
    """

    @staticmethod
    def collect_symbols(source: str) -> list[CodeSymbol]:
        """精确收集完整定义（含嵌套符号），不依赖索引 Chunk 覆盖范围。"""
        visitor = _SymbolVisitor()
        visitor.visit(ast.parse(source))
        return visitor.symbols

    @staticmethod
    def _module_path(relative_path: str) -> str:
        # Loader 一般用 /，兼容 Windows 路径分隔符。
        path = relative_path.replace("\\", "/")
        if path.lower().endswith(".py"):
            path = path[:-3]
        parts = [part for part in path.split("/") if part and part != "."]
        if parts and parts[-1] == "__init__":
            parts.pop()
        return ".".join(parts)

    @staticmethod
    def _overlap(start: int, end: int, symbol: CodeSymbol) -> int:
        """返回两段闭区间行号的交集长度。"""
        return max(0, min(end, symbol.end_line) - max(start, symbol.start_line) + 1)

    def enrich(self, source_doc: Document, chunks: list[Document]) -> list[Document]:
        relative_path = str(source_doc.metadata.get("relative_path", ""))
        module_path = self._module_path(relative_path)

        # 所有文件（包括 Markdown）统一设置字段，方便 PromptTemplate 使用。
        for chunk in chunks:
            chunk.metadata.update({
                "module_path": module_path if source_doc.metadata.get("file_type") == ".py" else "",
                "symbol_name": "",
                "symbol_type": "module" if source_doc.metadata.get("file_type") == ".py" else "document",
                "parent_class": "",
                "qualified_name": "",
                "symbol_names": "[]",  # Chroma 兼容：使用 JSON 字符串而非 list
            })

        if source_doc.metadata.get("file_type") != ".py" or not chunks:
            return chunks

        try:
            tree = ast.parse(source_doc.page_content)
        except (SyntaxError, ValueError) as exc:
            logger.warning("AST 解析失败，保留原始 Chunk：%s | %s", relative_path, exc)
            return chunks

        visitor = _SymbolVisitor()
        visitor.visit(tree)
        symbols = visitor.symbols
        logger.debug("AST 分析：%s，共 %d 个符号", relative_path, len(symbols))

        for chunk in chunks:
            start = int(chunk.metadata["start_line"])
            end = int(chunk.metadata["end_line"])
            matches = [s for s in symbols if self._overlap(start, end, s) > 0]
            if not matches:
                continue
            # 优先函数/方法，再按重叠范围选择最主要的符号。
            candidates = [s for s in matches if s.symbol_type != "class"] or matches
            primary = max(candidates, key=lambda s: (
                self._overlap(start, end, s),
                s.qualified_name.count("."),
            ))
            chunk.metadata.update({
                "symbol_name": primary.name,
                "symbol_type": primary.symbol_type,
                "parent_class": primary.parent_class,
                "qualified_name": primary.qualified_name,
                "symbol_names": json.dumps(
                    sorted({s.qualified_name for s in matches}),
                    ensure_ascii=False,
                ),
            })
        return chunks
