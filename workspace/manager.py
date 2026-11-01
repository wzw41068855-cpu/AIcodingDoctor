"""在干净提交上建立可审查 worktree，不更改源分支或暂存区。"""

import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess

from audit.writer import AuditWriter


def safe_relative(relative: str) -> tuple[str, ...]:
    """检查任务文件路径；在读取字节之前拒绝凭据、隐藏文件和越界。

    对应动机：
    not relative：拒绝空路径。
    "\\" in relative、":" in relative：拒绝 Windows 分隔符/盘符风格，统一成仓库内 POSIX 相对路径，减少绕过。
    is_absolute()：拒绝绝对路径，防止越界。
    控制字符检查：防不可见字符混淆。
    "" / "." / ".."：防目录穿越与异常分段。
    p.startswith(".")：拒绝隐藏路径段（如 .git、.env 家族）。
    p.lower() in sensitive 与 stem.lower() in sensitive：拒绝敏感命名（含带后缀变体，如 api_key.txt）。
    敏感后缀：拒绝证书/私钥常见格式（.pem/.key/.p12/.pfx）。

    Args:
        relative: 使用正斜杠的仓库相对路径。
    Returns:
        经过检查的路径分量。
    Raises:
        ValueError: 路径包含敏感名称、父目录、盘符或控制字符。
    """
    parts = relative.split("/")
    sensitive = {"secret", "secrets", "credentials", "credential", "id_rsa", "id_ed25519",
                 "key", "keys", "api_key", "private_key"}
    if (not relative or "\\" in relative or ":" in relative or PurePosixPath(relative).is_absolute()
            or any(ord(c) < 32 for c in relative)
            or any(p in {"", ".", ".."} or p.startswith(".") or p.lower() in sensitive for p in parts)
            or Path(parts[-1]).stem.lower() in sensitive
            or Path(parts[-1]).suffix.lower() in {".pem", ".key", ".p12", ".pfx"}):
        raise ValueError("拒绝不安全或敏感文件路径")
    return tuple(parts)


def git(root: Path, *arguments: str) -> str:
    """执行内部固定 Git 操作，禁用 hooks、过滤器和外部命令扩展。
        简单来说就是在指定的仓库目录下执行 git 命令，并且禁用了一些可能影响 git 行为的环境变量和配置，以确保 git 操作的安全性和可预测性。

    Args:
        root: 已绑定的本地仓库或 worktree。
        *arguments: 仅由内部函数构造的参数，不作为通用命令工具公开。
    Returns:
        UTF-8 标准输出，保留 NUL 以安全解析文件名。
    Raises:
        RuntimeError: Git 失败；不把原始输出和仓库内容带入异常。
        subprocess.TimeoutExpired: Git 超过 30 秒。
    """
    # 安全意图：
    # 去掉GIT_ *：避免宿主环境注入Git行为（pager、helper、hook等）。
    # 去掉疑似密钥变量：减少意外泄露 / 副作用。
    # 关闭系统与全局配置：让行为尽量由当前调用参数决定。
    # 禁止终端交互：防止Git等待输入而卡住。
    # 跳过LFS smudge：避免额外拉取动作。
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("GIT_") and not any(x in k.upper() for x in ("KEY", "TOKEN", "SECRET", "PASSWORD"))}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_LFS_SKIP_SMUDGE="1")

    # 前缀参数prefix：
    # 作用分别是：
    # core.hooksPath = < null >：禁hooks。
    # core.fsmonitor = false：禁fsmonitor。
    # core.pager = cat：避免分页器改写输出。
    # diff.external =：禁外部diff程序。
    # protocol.allow = never：禁协议访问（防网络 / 外部传输通道）。
    # -C root：强制在绑定仓库上下文执行。
    prefix = ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false",
              "-c", "core.pager=cat", "-c", "diff.external=", "-c", "protocol.allow=never", "-C", str(root)]
    names = subprocess.run(prefix + ["config", "--local", "--name-only", "--get-regexp", r"^filter\..*"],
                           env=env, capture_output=True, timeout=30)

    #动态禁用本地 filter.*
    filters = {s.split(".")[1] for s in names.stdout.decode("utf-8", "replace").splitlines()
               if re.fullmatch(r"filter\.[A-Za-z0-9_-]+\.[A-Za-z_]+", s)}
    for name in filters:
        prefix[1:1] = ["-c", f"filter.{name}.process=", "-c", f"filter.{name}.smudge=",
                       "-c", f"filter.{name}.clean=", "-c", f"filter.{name}.required=false"]

    result = subprocess.run(prefix + list(arguments), env=env, capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError("Git 操作失败，请核对仓库和基线")
    return result.stdout.decode("utf-8", "strict")


class WorktreeManager:
    """绑定一个提交和任务目录，默认保留修复结果。

    Attributes:
        root: 任务 worktree；必须在源仓库及开发仓库之外。
        base: 创建时解析的固定提交 SHA。
        artifacts: 工作树之外的任务产物目录。
    """

    def __init__(self, repository: Path, task_dir: Path, audit: AuditWriter):
        """绑定路径；任务目录由调用者新建且不与源仓库互相包含。

        Args:
            repository: 明确授权的本地可信 Git 仓库。
            task_dir: 已创建的独立任务产物目录。
            audit: 创建与清理工作树前必须写入的审计对象。
        Raises:
            ValueError: 根目录不是仓库根、路径为链接或目录互相包含。
        """
        supplied = Path(repository).absolute()
        target = Path(task_dir).absolute()#把传入路径转成绝对路径对象：
        if any(p.is_symlink() or p.is_junction() for p in [supplied, *supplied.parents, target, *target.parents]):
            raise ValueError("拒绝链接形式的仓库或任务目录")#连接可以做路径重定向，导致不安全的路径访问，所以拒绝使用符号链接或连接点。
        self.repository, self.artifacts = supplied.resolve(strict=True), target.resolve(strict=True)
        if self.artifacts.is_relative_to(self.repository) or self.repository.is_relative_to(self.artifacts):
            raise ValueError("任务目录必须与源仓库隔离")#避免任务目录和源仓库互相包含，防止潜在的安全问题或数据污染。
        if Path(git(self.repository, "rev-parse", "--show-toplevel").strip()).resolve() != self.repository:
            raise ValueError("必须指定 Git 仓库根目录")#确保提供的仓库路径是 Git 仓库的根目录。
        self.root, self.audit, self.base = self.artifacts / "worktree", audit, ""

    def create(self, revision: str = "HEAD") -> Path:
        """从干净提交创建 detached worktree，先验证再执行审计副作用。

        Args:
            revision: HEAD 或明确的本地提交/引用，禁止命令行选项形式。
        Returns:
            新建 worktree 的绝对路径。
        Raises:
            ValueError: 源仓库有未提交内容、危险 tree 条目或基线无效。
            RuntimeError: Git 操作失败；失败现场不自动删除。
        """
        with self.audit.operation("worktree.create") as event:#审计操作上下文管理器，用于记录 worktree 创建事件
            if not revision or revision.startswith("-") or "\x00" in revision:
                #拦三类输入：
                # 空值（无意义）
                # 以 - 开头（避免被解释成 Git 选项）
                # 含 NUL（非法/潜在注入）
                raise ValueError("非法基线")
            if git(self.repository, "status", "--porcelain=v1", "--untracked-files=all"):
                #要求源仓库完全干净：只要有已跟踪改动或未跟踪文件就拒绝
                raise ValueError("源仓库必须干净；不会忽略未提交或未跟踪文件")
            self.base = git(self.repository, "rev-parse", "--verify", revision + "^{commit}").strip()
                #把 revision 解析为确定 commit SHA
            for entry in git(self.repository, "ls-tree", "-r", "-z", self.base).split("\x00"):
                if not entry:
                    continue
                info, name = entry.split("\t", 1)
                if info.split()[0] not in {"100644", "100755"}:
                    raise ValueError("V3 不支持符号链接或子模块")
                # 隐藏元文件不读取也不发送模型；凭据树条目在 checkout 前拒绝。
                if any(p.lower().startswith(".env") for p in name.split("/")):
                    raise ValueError("拒绝含已跟踪环境凭据文件的仓库")
                if not any(p.startswith(".") for p in name.split("/")):
                    safe_relative(name)
            git(self.repository, "worktree", "add", "--detach", str(self.root), self.base)
            marker = {"repository": str(self.repository), "base": self.base, "root": str(self.root)}
            (self.artifacts / "worktree.json").write_text(json.dumps(marker), encoding="utf-8")
            event["base_commit"] = self.base
        return self.root

    def cleanup(self) -> None:
        """仅移除归属匹配且干净的任务 worktree，拒绝丢弃任何修改。
            ：只删除“确认属于当前任务、且没有任何未审改动”的 worktree。
            所以它默认偏保守，宁可拒绝，也不冒险误删。
        Raises:
            ValueError: 标记、Git 归属或内容状态不匹配。
            RuntimeError: Git 清理失败；不使用强制删除。
        """
        with self.audit.operation("worktree.cleanup"):
            marker = json.loads((self.artifacts / "worktree.json").read_text(encoding="utf-8"))
            common = Path(git(self.root, "rev-parse", "--git-common-dir").strip())
            original = Path(git(self.repository, "rev-parse", "--git-common-dir").strip())
            common = common if common.is_absolute() else self.root / common
            original = original if original.is_absolute() else self.repository / original
            if (marker != {"repository": str(self.repository), "base": self.base, "root": str(self.root)}
                    or self.root.is_symlink() or self.root.is_junction()
                    or common.resolve() != original.resolve()):
                raise ValueError("工作树归属不匹配")
            if git(self.root, "status", "--porcelain=v1", "--untracked-files=all"):
                raise ValueError("工作树有修改，必须保留人工审查")
            git(self.repository, "worktree", "remove", str(self.root))
