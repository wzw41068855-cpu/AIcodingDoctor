

from bisect import bisect_right
import json

from langchain_core.documents import Document
from langchain_text_splitters import (
    PythonCodeTextSplitter,
    MarkdownTextSplitter,
    RecursiveCharacterTextSplitter,
)

from utils.logger import get_logger
from rag.splitter.ast_tagger import AstTagger
from rag.splitter.ast_segmenter import AstCodeSegmenter, SourceSegment, has_python_code

logger = get_logger("code_splitter")

# V0.2.2：Python 使用 AST-first，只有超长代码段再调用 LangChain 切分器。
class CodeSplitter:
    """
    AicodingDoctor 代码切分器。

    主要功能：
    1. 接收 Loader 返回的 Document 列表。
    2. 根据文件后缀选择合适的文本切分器。
    3. Python 文件先按 AST 定界，超长代码段再由 LangChain 细分。
    4. 保留源文件的原始元数据。
    5. 为每个 Chunk 添加准确的起止行号和字符位置。

    start_line / end_line 是首尾有效字符所在行号（均包含在范围中）。

    chunk的metadata：
        继承自doc：
                        "source": str(file_path),#绝对路径
                        "file_name": file_path.name,#文件名称
                        "relative_path": relative_path,#相对根目录路径
                        "file_type": file_path.suffix.lower(),#类型
        新增：
                    "chunk_index": chunk_index,#Chunk索引，从0开始递增
                    "start_index": start_index,#Chunk起始字符位置
                    "end_index": end_index,#Chunk结束字符位置
                    "start_line": start_line,#Chunk起始行号
                    "end_line": end_line,#Chunk结束行号

    """

    def __init__(
        self,
        chunk_size: int = 1000,
        chunk_overlap: int = 150,
    ):
        """
        初始化切分器.
        :param chunk_size:
            每个 Chunk 的目标最大字符数。
            默认使用 len() 计算长度。

        :param chunk_overlap:
            相邻 Chunk 希望保留的重叠字符数。
            实际重叠长度可能小于设置值。
        """

        if chunk_size <= 0:
            raise ValueError("chunk_size 必须大于 0")

        if not 0 <= chunk_overlap < chunk_size:
            raise ValueError(
                "chunk_overlap 必须 >= 0 且小于 chunk_size"
            )

        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.ast_tagger = AstTagger()  # Markdown/语法错误等回退路径沿用旧逻辑
        self.ast_segmenter = AstCodeSegmenter()

    def _create_splitter(self, file_type: str):
        """
        根据文件类型创建 LangChain 切分器。

        add_start_index=True:
            在切分结果的 metadata 中自动添加 start_index，
            表示 Chunk 在原始 Document.page_content
            中的起始字符偏移量。

        strip_whitespace=False:
            保留 Chunk 原有的空白字符。
            这样可以避免因自动去除缩进和换行而改变源码。

        keep_separator=True:
            尽可能保留用于切分的分隔符。
        """

        common_config = {
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "add_start_index": True,
            #start_index，表示片段在原始 Document 中的‘字符’起始位置。用于计算行号
            "strip_whitespace": False,#保留 Chunk 原有的空白字符。
            "keep_separator": True,#尽可能保留用于切分的分隔符。
        }

        if file_type == ".py":
            return PythonCodeTextSplitter(**common_config)

        if file_type == ".md":
            return MarkdownTextSplitter(**common_config)

        # 其他类型使用通用文本切分器
        return RecursiveCharacterTextSplitter(**common_config)

    @staticmethod
    def _get_line_starts(text: str) -> list[int]:
        """
        预先记录每一行的起始字符位置。因为换行在str中以 '\n' 表示

        例如：
            text = "abc\\ndef\\nxyz"
            即
            "abc
            ndef
            nxyz"
        对应：
            第 1 行从字符 0 开始
            第 2 行从字符 4 开始
            第 3 行从字符 8 开始

        返回：
            [0, 4, 8]

        这里按照 '\\n' 作为换行符进行定位。
        同时适用于常见的 LF 和 CRLF 文本。
        """
        positions = [0]

        for index, char in enumerate(text):
            if char == "\n":
                positions.append(index + 1)

        return positions

    @staticmethod
    def _offset_to_line(
        offset: int,
        line_starts: list[int],
    ) -> int:
        """
        需要给定行起始位置列表 line_starts，给定字符偏移量 offset，
        返回该offset对应的行号（从 1 开始）。
        将字符偏移量转换为行号（从 1 开始）。

        bisect_right 使用二分查找。

        例如：
            line_starts = [0, 4, 8]

            offset = 0 -> 第 1 行
            offset = 5 -> 第 2 行
            offset = 9 -> 第 3 行
        """

        return bisect_right(line_starts, offset)

    def _split_python_ast_first(
        self, doc: Document, segments: list[SourceSegment],
    ) -> list[Document]:
        """AST 定界 -> 长段由原 PythonCodeTextSplitter 二次切分。

        对小于 chunk_size 的完整函数，直接生成一个 Document。
        避免把几个独立的小函数拼成一个向量 Chunk。
        """
        content = doc.page_content

        splitter = self._create_splitter(".py")
        pieces: list[Document] = []

        for segment_index, segment in enumerate(segments):
            segment_text = content[segment.start_index:segment.end_index]
            metadata = dict(doc.metadata)
            metadata.update({
                "module_path": self.ast_tagger._module_path(
                    str(doc.metadata.get("relative_path", ""))
                ),
                "symbol_name": segment.symbol_name,
                "symbol_type": segment.symbol_type,
                "parent_class": segment.parent_class,
                "qualified_name": segment.qualified_name,
                # Chroma 兼容：存 JSON 字符串，统一满足 document_prompt。
                "symbol_names": json.dumps(
                    [segment.qualified_name] if segment.qualified_name else [],
                    ensure_ascii=False,
                ),
                "ast_segment_index": segment_index,
                "ast_segment_type": segment.symbol_type,
            })

            if len(segment_text) <= self.chunk_size:
                # 一个完整的短函数 / 类方法 -> 一个 Chunk。
                partials = [Document(page_content=segment_text, metadata={
                    **metadata, "start_index": 0,
                })]
            else:
                # 复用官方 PythonCodeTextSplitter，不重新实现递归切分。
                segment_doc = Document(page_content=segment_text, metadata=metadata)
                partials = splitter.split_documents([segment_doc])

            nonempty = [p for p in partials if has_python_code(p.page_content)]
            for part_index, part in enumerate(nonempty):
                # LangChain 的 start_index 相对 segment_doc，不是整个文件。
                local_offset = int(part.metadata["start_index"])
                absolute_start = segment.start_index + local_offset
                absolute_end = absolute_start + len(part.page_content)

                if content[absolute_start:absolute_end] != part.page_content:
                    raise ValueError("AST Chunk 与原始源码的位置不一致")

                part.metadata.update({
                    "start_index": absolute_start,
                    "ast_part_index": part_index,
                    "ast_part_count": len(nonempty),
                })
                pieces.append(part)

        return pieces

    def split(self, documents: list[Document]) -> list[Document]:
        """切分文件并保留准确的原始源码位置。

        .py：AST-first -> 超长语法区间二次 Chunk。
        .md：原 MarkdownTextSplitter（保持 V0.2.1 行为）。
        Python 语法错误：回退旧 PythonCodeTextSplitter。
        """
        chunks: list[Document] = []

        for doc in documents:
            content = doc.page_content
            if not content.strip():
                continue

            file_type = str(doc.metadata.get("file_type", "")).lower()
            is_ast_first = file_type == ".py"

            if is_ast_first:
                try:
                    # 只在 AST 解析/边界校验失败时回退旧策略。
                    segments = self.ast_segmenter.split(content)
                except (SyntaxError, ValueError) as exc:
                    logger.warning(
                        "AST-first 无法解析 %s，回退旧切分器：%s",
                        doc.metadata.get("relative_path", ""), exc,
                    )
                    split_docs = self._create_splitter(file_type).split_documents([doc])
                    is_ast_first = False
                else:
                    # LangChain 二次切分如果真正出错，应显式失败，不能悄悄掩盖。
                    split_docs = self._split_python_ast_first(doc, segments)
            else:
                split_docs = self._create_splitter(file_type).split_documents([doc])

            # 行号始终根据完整原始文件计算，而非临时 AST 区间。
            line_starts = self._get_line_starts(content)
            base_line = int(doc.metadata.get("start_line", 1))
            file_chunks: list[Document] = []

            for chunk in split_docs:
                text = chunk.page_content
                if not text.strip():
                    continue

                start_index = int(chunk.metadata["start_index"])
                end_index = start_index + len(text)
                if content[start_index:end_index] != text:
                    raise ValueError("Chunk 与原始文件字符范围不一致")

                # 准确行号采用首、尾两个非空白字符的位置。
                leading = len(text) - len(text.lstrip())
                trailing = len(text) - len(text.rstrip())
                effective_start = start_index + leading
                effective_end = end_index - trailing - 1

                start_line = base_line + self._offset_to_line(effective_start, line_starts) - 1
                end_line = base_line + self._offset_to_line(effective_end, line_starts) - 1

                chunk.metadata.update({
                    "chunk_index": len(file_chunks),  # 文件内从 0 连续编号
                    "start_index": start_index,
                    "end_index": end_index,
                    "start_line": start_line,
                    "end_line": end_line,  # 闭区间：有效字符所在的最后一行
                })
                file_chunks.append(chunk)

            if not is_ast_first:
                # Markdown / 非法 Python：继续使用旧版 AstTagger 填齐字段。
                self.ast_tagger.enrich(doc, file_chunks)

            chunks.extend(file_chunks)

        logger.info(
            "切分完成：%d 个文件 -> %d 个 Chunk（Python AST-first）",
            len(documents), len(chunks),
        )
        return chunks


if __name__ == "__main__":
    from rag.loader import Loader
    # 修改成你的本地项目路径
    project_path = r"C:\Users\wzw41\PycharmProjects\FastAPI_aiJueJIn"

    loader = Loader(project_path)

    documents = loader.load()

    splitter = CodeSplitter()

    result = splitter.split([documents[3]])

    for chunk in result[:3]:
        print("\n" + "=" * 50)
        print("Metadata:", chunk.metadata)
        print("Content:")
        print(repr(chunk.page_content))
