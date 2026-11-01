from pathlib import Path
import io
import tokenize

from langchain_core.documents import Document
from langchain_community.document_loaders import (
    PythonLoader,
    UnstructuredMarkdownLoader,
)
from langchain_community.document_loaders import TextLoader

from utils.logger import get_logger

logger = get_logger("loader")

class Loader:
    """
    AIcodingDoctor 代码加载器。

    功能：
    1. 递归遍历指定目录。
    2. 忽略 .git、venv、node_modules 等目录。
    3. 根据文件后缀选择不同的 LangChain Loader。
    4. 返回 Document 对象列表。
    5. 为每个 Document 的元数据添加文件路径、文件名和行号。

    Retriever 找到相关 Document 后，LLM 使用 page_content 理解代码，
    系统使用 metadata 定位来源。

    这里没有进行代码切分，切分在Splitter模块。
    doc的metadata：
                        "source": str(file_path),绝对路径
                        "file_name": file_path.name,名称
                        "relative_path": relative_path,相对根目录路径
                        "file_type": file_path.suffix.lower(),类型
                        "start_line": 1,
                        "end_line": line_count,
    """

    # 支持的文件类型
    SUPPORTED_SUFFIXES = {".py", ".md"}

    # 忽略的目录
    IGNORED_DIRS = {
        ".git",
        ".venv",
        ".env",
        "venv",
        "env",
        "node_modules",
        "__pycache__",
        ".idea",
        ".vscode",
        ".pytest_cache",
        "dist",
        "build",
    }

    def __init__(self, directory: str):
        # 转换为绝对路径，避免相对路径问题
        #expanduser()：展开 ~ 代表的用户主目录。
        # resolve()：生成规范化的绝对路径。
        self.directory = Path(directory).expanduser().resolve()

        if not self.directory.exists():
            raise FileNotFoundError(
                f"目录不存在：{self.directory}"
            )

        if not self.directory.is_dir():
            raise NotADirectoryError(
                f"不是有效目录：{self.directory}"
            )
        self.failed_files = []

    def resolve_path(self, path: str) -> Path:
        """校验只读入口路径，拒绝隐藏目录、敏感文件及解析后的越界路径。"""
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.directory / candidate
        try:
            relative = candidate.relative_to(self.directory)
        except ValueError:
            raise ValueError("路径不属于允许的仓库") from None
        blocked = {"secrets", "secret", "credentials", "credential", "id_rsa", "id_ed25519"}
        if (any(p.startswith(".") or p.lower() in self.IGNORED_DIRS or p.lower() in blocked
                for p in relative.parts)
                or candidate.stem.lower() in blocked
                or candidate.suffix.lower() not in self.SUPPORTED_SUFFIXES):
            raise ValueError("不允许读取此文件类型或目录")
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.directory):
            raise ValueError("路径或符号链接越出仓库")
        # 同时检查链接目标，避免通过别名访问隐藏或敏感文件。
        target = resolved.relative_to(self.directory)
        if (any(p.startswith(".") or p.lower() in self.IGNORED_DIRS or p.lower() in blocked
                for p in target.parts)
                or resolved.stem.lower() in blocked
                or resolved.suffix.lower() not in self.SUPPORTED_SUFFIXES):
            raise ValueError("链接目标不允许读取")
        return resolved

    def read_file(self, path: str, max_bytes: int = 1_048_576) -> Document:
        """限量读取当前磁盘文本；Python 使用声明编码，Markdown 使用 UTF-8。"""
        file_path = self.resolve_path(path)
        if not file_path.is_file():
            raise FileNotFoundError("源文件不存在或不是普通文件")
        with file_path.open("rb") as stream:
            raw = stream.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ValueError("文件超过读取大小限制")
        if b"\x00" in raw:
            raise ValueError("拒绝读取二进制文件")
        encoding = (tokenize.detect_encoding(io.BytesIO(raw).readline)[0]
                    if file_path.suffix.lower() == ".py" else "utf-8-sig")
        content = raw.decode(encoding).replace("\r\n", "\n").replace("\r", "\n")
        if any(ord(char) < 32 and char not in "\n\t\f" for char in content):
            raise ValueError("文件包含非文本控制字符")
        return Document(page_content=content, metadata={
            "source": str(file_path), "relative_path": file_path.relative_to(self.directory).as_posix(),
            "file_name": file_path.name, "file_type": file_path.suffix.lower(),
            "start_line": 1 if content else None,
            "end_line": content.count("\n") + (not content.endswith("\n")) if content else None,
        })

    def load_files(self, paths: list[str], max_bytes: int = 1_048_576) -> list[Document]:
        """加载显式文件列表，包括空文件；失败仅记录路径，由调用方决定是否提交。"""
        self.failed_files = []
        documents = []
        for path in paths:
            try:
                documents.append(self.read_file(path, max_bytes))
            except (OSError, ValueError, SyntaxError):
                self.failed_files.append(str(path))
        return documents

    def _should_ignore(self, file_path: Path) -> bool:
        """检查文件是否位于需要忽略的目录中。"""

        relative_path = file_path.relative_to(self.directory)

        # 检查路径中的每一级/目录，是否在忽略的目录中
        return any(
            part in self.IGNORED_DIRS
            for part in relative_path.parts[:-1]
        )

    def _create_loader(self, file_path: Path):
        """根据文件后缀创建对应的加载器。"""

        suffix = file_path.suffix.lower()#后缀

        if suffix == ".py":
            return PythonLoader(str(file_path))

        if suffix == ".md":
            return TextLoader(#按照MD源码解析行号
                str(file_path),
                encoding="utf-8",
            )
            #return UnstructuredMarkdownLoader(
            #    str(file_path),
            #    mode="single",
            #)

        return None

    def load(self) -> list[Document]:
        """加载目录下所有支持的源文件。"""

        documents: list[Document] = []

        # rglob("*") 递归遍历所有文件及目录
        for file_path in sorted(self.directory.rglob("*")):
        #self.directory.rglob("*") 返回的路径是相对路径还是绝对路径，
        # 取决于 self.directory 本身是相对路径还是绝对路径。
        # 因为  这里的self.directory 是绝对路径，返回的路径也是绝对路径。

            # 只处理普通文件
            if not file_path.is_file():
                continue

            # 忽略不支持的文件类型
            if file_path.suffix.lower() not in self.SUPPORTED_SUFFIXES:
                continue

            # 忽略指定目录中的文件
            if self._should_ignore(file_path):
                continue

            try:
                loader = self._create_loader(file_path)

                if loader is None:
                    continue

                # LangChain Loader 返回 Document 列表
                docs = loader.load()

                # 获取相对于项目根目录的路径
                relative_path = file_path.relative_to(
                    self.directory
                ).as_posix()

                # 为每个 Document 补充元数据
                for doc in docs:

                    # 1. 跳过空文件或只有空白字符的文件
                    if not doc.page_content.strip():
                        logger.debug(f"[跳过空文件] {relative_path}")
                        continue

                    # 获取文本总行数
                    line_count = len(doc.page_content.splitlines())

                    # 为当前 Document 补充可追溯元数据，供检索结果展示、来源定位与后续切分/高亮使用。
                    doc.metadata.update({
                        "source": str(file_path),  # 文件绝对路径；用于精确定位磁盘来源。
                        "file_name": file_path.name,  # 文件名（不含目录）；便于在 UI 或日志中简洁展示。
                        "relative_path": relative_path,  # 相对项目根目录路径；跨环境更稳定、可读性更好。
                        "file_type": file_path.suffix.lower(),  # 文件扩展名（小写）；用于按类型过滤/路由处理逻辑。
                        "start_line": 1,  # 文档在源文件中的起始行号；整文件加载时固定从第 1 行开始。
                        "end_line": line_count,  # 文档在源文件中的结束行号；等于当前内容总行数。
                    })

                    documents.append(doc)

                logger.debug(f"[成功] {relative_path}")

            except Exception as e:
                # 单个文件读取失败，不影响其他文件
                self.failed_files.append(str(file_path))
                logger.debug(f"[失败] {file_path}: {e}")

        logger.info(f"\n加载完成，共生成 {len(documents)} 个 Document")

        return documents


if __name__ == "__main__":

    # 修改成你的本地项目路径
    project_path = r"C:\Users\wzw41\PycharmProjects\FastAPI_aiJueJIn"

    loader = Loader(project_path)

    documents = loader.load()

    if documents:
        doc = documents[0]

        print("\n===== 第一个 Document =====")
        print("metadata:", doc.metadata)
        print("page_content:")
        print(doc.page_content[:500])

