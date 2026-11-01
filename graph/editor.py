"""V2 工作区权限与确定性替换，不接收任意 Shell 或模型文件写入工具。"""

import ast
import difflib
import hashlib
import io
import os
from pathlib import Path, PureWindowsPath
import tempfile
import tokenize

import config
from graph.state import ChangeRecord, EditBatch
from services.code_search import CodeSearchService
from utils.operation_timing import timed


def sha256(raw: bytes) -> str:
    """计算原始字节版本摘要，供读取与提交之间的冲突检测。

    Args:
        raw: 未做换行或编码转换的文件内容。
    Returns:
        十六进制 SHA256 字符串。
    """
    return hashlib.sha256(raw).hexdigest()


class Workspace:
    """绑定调用者明确授权的可信隔离副本。（不是源文件，是源文件的副本）
    Workspace 是这个修复系统的安全边界对象。它不负责改代码本身，
    而是负责回答“这个路径/文件是否允许被本次任务访问或编辑”。
    它采用的是“强输入校验 + 明确白名单”而不是运行时沙箱隔离。

    不自动复制任意仓库，也不把受控 subprocess 视为沙箱。

    Attributes:
        root: 解析后的已有副本目录。
        max_bytes: 单文件读取和编辑上限，默认 64 KiB。
        service: 仅用于路径和实时读取的 V1 服务，不初始化真实索引。
    """

    def __init__(self, root: str | Path, *, trusted_copy: bool, max_bytes: int = 65536):
        """校验显式信任声明和副本位置。

        Args:
            root: 调用者指定的已有测试副本。
            trusted_copy: 必须显式为 True，表示确认副本来源可信。
            max_bytes: 每个源文件的最大字节数，1 到 65536。
        Raises:
            ValueError: 未授权、目录敏感、指向开发仓库或参数越界。
            OSError: 目录不存在或无法访问。
        """
        supplied = Path(root).absolute()#把用户传入路径做绝对化和严格解析：
        self.root = supplied.resolve(strict=True)
        project = Path(config.BASE_DIR).resolve()
        if (trusted_copy is not True or not self.root.is_dir()
                or self.root == Path.home().resolve()
                or project.is_relative_to(self.root) or self.root.is_relative_to(project)
                or self.root == Path(self.root.anchor)
                or any(p.startswith(".") for p in self.root.parts[1:])
                or any(p.lower() in {"windows", "program files", "programdata", "system32"}
                       for p in self.root.parts)
                # 这些条件背后有明确安全意图：
                # 必须显式 trusted_copy=True，防止调用方“默认信任”；
                # 不能指向用户主目录、系统敏感目录、磁盘根目录；
                # 不能与开发仓库互相包含（防止误改源码仓库本体）；
                # 路径链路上不能有符号链接 / 联接，避免路径欺骗；
                # 限制 max_bytes 在 1..65536，防止超大文件处理风险。
                or any(p.is_symlink() or p.is_junction() for p in [supplied, *supplied.parents])):
            raise ValueError("必须指定开发仓库之外的可信隔离副本，拒绝敏感目录和链接")
        if type(max_bytes) is not int or not 1 <= max_bytes <= 65536:
            raise ValueError("文件字节上限必须在 1 到 65536 之间")
        self.max_bytes = max_bytes
        self.service = CodeSearchService(self.root, max_file_bytes=max_bytes)

    def path(self, relative: str, *, source: bool = True) -> Path:
        """校验相对路径及每一级链接，已有路径才可使用。
        path()：检查文件路径是否合法
        Args:
            relative: 不含绝对前缀、冒号和父目录分量的相对路径。
            source: True 时只允许已有业务 Python 文件，禁止测试和配置。
        Returns:
            工作区内的已有路径。
        Raises:
            ValueError: 路径越界、链接、敏感文件或非业务源码。
            OSError: 文件不存在。
        """
        parts = relative.replace("\\", "/").split("/")
        if self.root.is_symlink() or self.root.is_junction() or self.root.resolve() != self.root:
            raise ValueError("工作区位置已改变")
        if (not relative or Path(relative).is_absolute() or PureWindowsPath(relative).drive
                or ":" in relative or any(p in {"", ".", ".."} for p in parts)
                or any(p.startswith(".") or p.lower() in self.service.loader.IGNORED_DIRS
                       or p.lower() in {"secrets", "credentials", "secret", "credential"}
                       for p in parts)):
            raise ValueError("拒绝不安全相对路径")
        candidate = self.root.joinpath(*parts)
        for part in [candidate, *candidate.parents]:
            if part == self.root:
                break
            if part.is_symlink() or part.is_junction():
                raise ValueError("拒绝符号链接或目录联接")
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(self.root):
            raise ValueError("路径越出工作区")
        if source:
            self.service.loader.resolve_path(relative)
            if (resolved.suffix != ".py" or not resolved.is_file()
                    or resolved.name in {"conftest.py", "setup.py", "config.py", "settings.py",
                                         "key.py", "keys.py", "api_key.py", "private_key.py"}
                    or resolved.stat().st_nlink > 1
                    or resolved.name.startswith("test_") or resolved.name.endswith("_test.py")
                    or any(p.lower() in {"test", "tests"} for p in parts[:-1])):
                raise ValueError("仅允许编辑已有业务 .py 文件，禁止修改验收测试")
        return resolved


class Editor:
    """验证整批替换后提交，保存任务初始字节用于最终 Diff。

    Attributes:
        originals: 首次成功修改前的原始字节，最多八个文件。
        workspace: 固定工作区权限对象。
    """

    def __init__(self, workspace: Workspace):
        """绑定权限对象，初始化空的任务级原始快照。

        Args:
            workspace: 经显式信任与隔离校验的副本。
        """
        self.workspace = workspace
        self.originals: dict[str, bytes] = {}#	保存文件首次成功修改前的原始字节

    @timed("编辑器：读取并校验最新快照")
    def snapshot(self, path: str) -> dict:
        """在coder中把代码发送给LLM前生成快照。
        通过 V1 读取最新文本，并校验其与原始字节属于同一版本。

        项目重点：每轮 Coder 都重新获取快照。不能拿第一轮的文件版本反复修改。

        Args:
            path: 已有业务 Python 相对路径。
        Returns:
            含 path、content、expected_hash 的模型编辑证据。
        Raises:
            ValueError: 超大、二进制、并发修改或超过 1000 行。
        """
        target = self.workspace.path(path)
        raw = target.read_bytes() if target.stat().st_size <= self.workspace.max_bytes else b""
        context = self.workspace.service.get_code_context(path)
        if len(context.content.split("\n")) > 1000:
            raise ValueError("文件超过 1000 行读取上限")
        if target.read_bytes() != raw:
            raise ValueError("读取期间文件发生变化")
        return {"path": path, "content": context.content, "expected_hash": sha256(raw)}

    @timed("编辑器：校验与应用补丁")
    def apply(self, batch: EditBatch, allowed: list[str]) -> list[ChangeRecord]:
        """整批验证唯一匹配、版本、编码和语法，再原子替换每个文件。
        先验证、后暂存、再原子替换、失败可回滚。
        Args:
            batch: 模型输出经 Pydantic 验证后的替换块。
            allowed: Planner 校验通过的目标文件白名单。
        Returns:
            成功应用的变更哈希记录。
        Raises:
            ValueError: 无效或重复路径、版本冲突、匹配不唯一、语法错误。
            OSError: 提交失败；尝试恢复本轮已提交文件。
        Notes:
            单个文件使用 os.replace；多文件不具备操作系统级事务。
            提交失败会恢复本轮内容；恢复失败明确抛出 RuntimeError。
        """
        prepared = {}
        #prepared 是 Editor.apply() 里的字典，
        # 用来暂存本轮已通过验证、准备提交的文件修改内容，稍后再统一写入：

        for edit in batch.edits:
            target = self.workspace.path(edit.path)#验证路径合法性和存在性：
            if edit.path not in allowed or target in prepared:#它先检查路径是否在白名单中，且单文件在一轮里不允许重复替换：
                raise ValueError("补丁不在计划白名单或单文件存在重复替换")
            snap = self.snapshot(edit.path)
            raw = target.read_bytes()#然后用快照与哈希做版本一致性检查，避免“基于旧版本补丁”覆盖新文件：
            if sha256(raw) != edit.expected_hash or snap["expected_hash"] != edit.expected_hash:
                raise ValueError("文件版本冲突")
            old = snap["content"]
            if old.count(edit.old_text) != 1 or edit.old_text == edit.new_text:#接着验证替换文本必须“唯一命中且有实际变化”：
                raise ValueError("旧文本必须唯一匹配，且替换必须改变内容")
            new = old.replace(edit.old_text, edit.new_text, 1)
            # 语法检查不执行代码；不能用模型判断替代解析器验证。
            ast.parse(new, filename=edit.path)#替换后先做 Python 语法检查（只 parse，不执行）

            #这三行是在把替换后的源码 new 转回文件字节时，尽量保留原文件的编码和换行格式：
            encoding = tokenize.detect_encoding(io.BytesIO(raw).readline)[0]
            newline = "\r\n" if b"\r\n" in raw else "\n"
            encoded = new.replace("\n", newline).encode(encoding)
            if len(encoded) > self.workspace.max_bytes or b"\x00" in encoded:
                raise ValueError("替换后文件过大或含二进制内容")
            prepared[target] = (edit.path, raw, encoded)
        if len(set(self.originals) | {v[0] for v in prepared.values()}) > 8:
            raise ValueError("任务最多修改八个文件")
        # 在提交前保留基线，极端回滚失败时最终报告仍能展示残留修改。

        #原子替换与竞争窗口防护。
        #真正替换前会再次确认“路径仍一致 + 原文件字节没变”：
        #把前面验证好的修改写入文件，并在提交中途出错时尽量回滚：
        for path, raw, _ in prepared.values():
            self.originals.setdefault(path, raw)#保存任务基线
        staged, committed = {}, []
        try:
            for target, (_, raw, encoded) in prepared.items():
                fd, name = tempfile.mkstemp(prefix=".v2-edit-", dir=target.parent)
                staged[target] = Path(name)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(encoded)#先写临时文件
                    stream.flush()#缓冲区刷入磁盘
                    os.fsync(stream.fileno())#确保写入磁盘
                os.chmod(staged[target], target.stat().st_mode)
            for target, (path, raw, _) in prepared.items():
                # 写入前再次检查路径和原始字节，缩小外部修改造成的竞争窗口。
                if self.workspace.path(path) != target or target.read_bytes() != raw:
                    raise ValueError("提交期间文件发生变化")
                os.replace(staged[target], target)#用 os.replace() 原子替换目标文件
                committed.append(target)
        except BaseException:
            for target in reversed(committed):
                try:
                    target.write_bytes(prepared[target][1])#回滚已提交的文件到原始字节
                except OSError as exc:
                    raise RuntimeError("批量提交失败且回滚失败，需人工检查副本") from exc
            raise#在 except 块中重新抛出刚刚捕获的原始异常。
        finally:
            for temp in staged.values():
                temp.unlink(missing_ok=True)
        records = []
        for path, raw, encoded in prepared.values():
            records.append(ChangeRecord(path=path, before_hash=sha256(raw), after_hash=sha256(encoded)))
        return records

    @timed("编辑器：核对最终文件变更")
    def current_changes(self) -> list[ChangeRecord]:
        """读取任务基线与当前字节，报告净修改及回滚失败后的残留修改。

        Returns:
            实际字节不同的文件哈希记录，不包含已完全恢复的文件。改过又恢复原样的文件不会被报告
        Raises:
            OSError: 原目标文件已被外部移除或无法访问。
        """
        records = []
        for path, before in self.originals.items():
            after = self.workspace.path(path).read_bytes()
            if before != after:
                records.append(ChangeRecord(path=path, before_hash=sha256(before), after_hash=sha256(after)))
        return records

    @timed("编辑器：生成最终 Diff")
    def diff(self) -> str:
        """生成相对任务起点的最终统一 Diff，只读取已记录的最多八个文件。

        diff() 的职责是生成“相对于任务起点”的统一差异文本，给人审查最终改动。
        它依赖 self.originals（首次修改前保存的原始字节）作为基线，而不是依赖本轮 patch 内容，
        因此即使经历多轮修改/回滚，结果仍能反映“净改动”。
        内部使用 Python 标准库：
            difflib.unified_diff(...)
        生成 Git 常见风格的 Unified Diff。

        Returns:
            可供人工审查的统一 Diff；未修改时为空字符串。
        Raises:
            OSError: 修改后的文件无法读取。
        """
        diffs = []
        for relative, raw in self.originals.items():
            target = self.workspace.path(relative)#取得当前文件路径。
            encoding = tokenize.detect_encoding(io.BytesIO(raw).readline)[0]
            before = raw.decode(encoding).replace("\r\n", "\n")
            after = target.read_bytes().decode(encoding).replace("\r\n", "\n")
            for line in difflib.unified_diff(before.splitlines(keepends=True),#生成该文件的 unified diff：
                                             after.splitlines(keepends=True),#保留每行末尾的换行，避免比较时丢失换行信息。
                                             fromfile=f"a/{relative}", tofile=f"b/{relative}"):#设置 Diff 头部显示的旧、新文件路径。
                # 无末尾换行的源文件也必须生成可辨认的两行修改记录。
                diffs.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
        return "".join(diffs)
