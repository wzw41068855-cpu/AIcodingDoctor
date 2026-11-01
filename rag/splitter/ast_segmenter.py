"""AST-first 的第一阶段：按 Python 语法定义边界定位源码区间。

这个模块只依赖 Python 标准库，不生成 Document，也不调用 Embedding。
返回的 [start_index, end_index) 均指向原始源码（不是 ast.unparse() 的结果）。
这样 CodeSplitter 可以继续让 LangChain 处理超长区间，同时保持原有字符偏移与行号。
"""

import ast
import io
import re
import tokenize
from dataclasses import dataclass


def has_python_code(content: str) -> bool:
    """判断一段内容是否只有注释.如果只有注释，返回false
    仅过滤可以可靠识别的纯注释/空白；保留字符串及不完整代码。"""
    ignored = {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE,
               tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER,
               tokenize.ENCODING}#ignored = {...} 建立一个集合，表示可以忽略的 Token 类型(Python 词法分析产生的标记)。
    try:
        return any(token.type not in ignored for token in
                   tokenize.generate_tokens(io.StringIO(content).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return True


@dataclass(frozen=True)
class SourceSegment:
    """一段连续、不重叠的原始源码区间，使用左闭右开字符偏移。
    symbol_name 是短名称，qualified_name 是完整限定名称。
    例如类方法：symbol_name 是方法名，qualified_name 是类名.方法名。
    例子
    segment = SourceSegment(
        start_index=100,
        end_index=250,
        symbol_name="create_token",
        symbol_type="function",
        qualified_name="create_token",
)

    """

    start_index: int
    end_index: int
    symbol_name: str = ""#symbol_name 是短名，比如函数名 foo、方法名 bar；
    symbol_type: str = "module"
    #symbol_type 用来标记片段类别，默认 "module"，表示模块级片段（例如 import、常量、散落语句），而不是函数/方法体。
    qualified_name: str = ""#完整路径名，用于唯一定位语义位置。例如 MyClass.run 或 pkg.mod.func 这种层级标识
    parent_class: str = ""#parent_class 则专门记录所属类上下文，便于区分“同名函数但不同类”的情况


class AstCodeSegmenter:
    """先按函数/类的语法边界分段；过长段由 CodeSplitter 再次切分。

    设计原则：
    - 模块顶层函数、类方法、嵌套类都可成为独立代码段。
    - 函数内部的嵌套函数先保留在外层函数中，避免重复索引。
    - 类体里的属性、文档字符串、注释、空行也保留为类级段。
    - 没有归属函数的 import、常量、语句保留为 module 段。
    - 装饰器行属于它修饰的函数或类。
    - 所有区间都是原始源码切片，不拼接、删改或重新格式化代码。
    """

    @staticmethod
    def _node_start_line(node: ast.AST) -> int:
        """函数/类装饰器要与声明放在同一段，故取最早装饰器行。
        为什么要单独处理装饰器？
        AST 中 FunctionDef 或 AsyncFunctionDef 的 lineno 指向函数定义行，而装饰器位于前面的行。
        如果直接使用 node.lineno，就可能把装饰器留在函数 Chunk 外面。
        这避免了 @decorator 被切到上一个片段，导致函数定义和装饰器分离。对检索与重建上下文来说，这是非常重要的完整性保证。
        """
        start = node.lineno
        decorators = getattr(node, "decorator_list", [])
        if decorators:
            start = min(start, *(decorator.lineno for decorator in decorators))
        return start

    def split(self, source: str) -> list[SourceSegment]:
        """返回按源码顺序排列的语法区间；语法错误交给上层回退。"""
        tree = ast.parse(source)#ast.parse() 将 Python 源码转换为抽象语法树（AST）。

        # Python 源码行以 LF/CRLF/CR 定界，字符串里的 U+2028、\f 等
        # 不是源码换行。str.splitlines() 会误拆这些字符，破坏 AST 行号映射。
        lines = re.findall(r"[^\r\n]*(?:\r\n|\r|\n|$)", source)#这行是把源码按真正的换行符拆成一个列表，同时保留换行符。
        if lines and not lines[-1]:
            lines.pop()
        # tokenize 防止把多行字符串内的 # 误认为说明性注释。
        comments = {
            token.start[0]: token for token in
            tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type == tokenize.COMMENT
            and not lines[token.start[0] - 1][:token.start[1]].strip()
        }

        #protected：保护特殊注释
        # 这里要防止以下注释被错误地归入某个函数：
        # # !/usr/bin/env python3# -*- coding: utf-8 -*-# Copyright ...# SPDX-License-Identifier: MIT
        #
        # 还有一些工具指令：
        # # noqa# type: ignore# fmt: off
        #
        # 它们可能影响解释器、静态检查器或格式化器的行为。
        protected = re.compile(
            r"^#!|coding\s*[:=]|copyright|license|licence|SPDX|"
            r"all rights reserved|许可证|版权|\bnoqa\b|\btype:\s*ignore|"
            r"\b(?:fmt|isort|ruff|pylint|pyright)\s*:", re.IGNORECASE,
        )

        # 修复注释被单独分离的问题
        def include_comments(start: int, minimum: int, indent: int) -> int:
            candidate = start
            i = start - 1
            while i >= minimum:
                if not lines[i - 1].strip():
                    i -= 1
                    continue
                comment = comments.get(i)
                if comment is None or comment.start[1] != indent:
                    break
                if protected.search(comment.string):
                    # 文件头/工具指令所属的连续块不移入函数。
                    return start
                candidate = i
                i -= 1
            return candidate
        line_offsets = [0]
        for line in lines:
            line_offsets.append(line_offsets[-1] + len(line))

        segments: list[SourceSegment] = []

        #真正创建源码结构段
        def emit(first_line: int, end_line_exclusive: int, *,
                 symbol_name: str = "", symbol_type: str = "module",
                 qualified_name: str = "", parent_class: str = "") -> None:
            """按行号取原始源码，忽略完全空白的区间。"""
            if first_line >= end_line_exclusive:
                return
            start = line_offsets[first_line - 1]
            end = line_offsets[end_line_exclusive - 1]
            if not source[start:end].strip():
                return
            segments.append(SourceSegment(
                start_index=start,
                end_index=end,
                symbol_name=symbol_name,
                symbol_type=symbol_type,
                qualified_name=qualified_name,
                parent_class=parent_class,
            ))

        def visit_scope(first_line: int, end_line_exclusive: int,
                        statements: list[ast.stmt], *,
                        scope_kind: str = "module", scope_name: str = "",
                        qualified_name: str = "", parent_class: str = "") -> None:
            """模块/类的直接子语句中，仅按 def/class 切边界。

            其他语句（如 import、赋值、docstring、注释）不会丢弃，
            而是归入相邻定义间的 module 或 class 范围。
            """
            cursor = first_line
            previous_sibling_end = first_line - 1
            for node in statements:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    previous_sibling_end = node.end_lineno
                    continue

                node_start = include_comments(
                    self._node_start_line(node),
                    max(cursor, previous_sibling_end + 1), node.col_offset,
                )
                node_end_exclusive = node.end_lineno + 1

                # 当前作用域内的前置/间隔文本：模块 import、类属性、注释等。
                emit(cursor, node_start,
                     symbol_name=scope_name,
                     symbol_type=scope_kind,
                     qualified_name=qualified_name,
                     parent_class=parent_class)

                child_qualified = (
                    f"{qualified_name}.{node.name}" if qualified_name else node.name
                )

                if isinstance(node, ast.ClassDef):
                    # 类不是一次性整体入库：类头/类属性 + 各方法独立划分。
                    # 嵌套类同样递归处理；不能把整个类与每个方法重复入库。
                    visit_scope(
                        node_start, node_end_exclusive, node.body,
                        scope_kind="class", scope_name=node.name,
                        qualified_name=child_qualified,
                        parent_class=qualified_name if scope_kind == "class" else parent_class,
                    )
                else:
                    # 类的直接子函数属于 method，模块顶层属于 function。
                    # 函数内部不递归切分，避免重复索引嵌套函数的正文。
                    emit(
                        node_start, node_end_exclusive,
                        symbol_name=node.name,
                        symbol_type="method" if scope_kind == "class" else "function",
                        qualified_name=child_qualified,
                        parent_class=qualified_name if scope_kind == "class" else parent_class,
                    )
                cursor = node_end_exclusive
                previous_sibling_end = node.end_lineno

            emit(cursor, end_line_exclusive,
                 symbol_name=scope_name,
                 symbol_type=scope_kind,
                 qualified_name=qualified_name,
                 parent_class=parent_class)

        # 模块级范围涵盖整个文件；最后一行可能是以换行结束的空行。
        visit_scope(1, len(lines) + 1, tree.body)

        # 按构造逻辑，每段不重叠；验证有内容的字符均落在某一段。
        # 段与段之间允许只有空白（这些空白不影响检索）。
        previous_end = 0
        for segment in segments:
            if segment.start_index < previous_end:
                raise ValueError("AST 段出现重复覆盖，请检查代码范围")
            if source[previous_end:segment.start_index].strip():
                raise ValueError("AST 分段遗漏了非空白源码")
            previous_end = segment.end_index
        if source[previous_end:].strip():
            raise ValueError("AST 分段遗漏了文件尾部的源码")

        # 先验证完整边界，再省略无代码区间；文档字符串仍然保留。
        return [segment for segment in segments if has_python_code(
            source[segment.start_index:segment.end_index]
        )]
