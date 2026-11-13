"""将经过过滤的测试输入放入非特权、断网、限额的 Linux 容器。"""

import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from threading import Event, Thread
from time import monotonic
from uuid import uuid4
import xml.etree.ElementTree as ET

from graph.state import TestResult
from graph.test_runner import PytestRunner, redact
from utils.operation_timing import timed
from workspace.manager import git, safe_relative


# 此引导代码来自工具自身，不接受模型指定程序、参数或入口点。
_BOOTSTRAP = """import json, os, shutil, subprocess, sys
from pathlib import Path
for item in Path('/input').iterdir():
    target = Path('/workspace') / item.name
    if item.is_dir():
        shutil.copytree(item, target)
    else:
        shutil.copy2(item, target)
os.chdir('/workspace')
args = [sys.executable, '-m', 'pytest', *json.loads(sys.argv[1]), '-q', '-o', 'addopts=',
        '-p', 'no:cacheprovider', '--junitxml=/reports/junit.xml']
raise SystemExit(subprocess.call(args))
"""


def capture(command: list[str], timeout: float, limit: int = 16384,
            cancel: Event | None = None) -> tuple[int, bytes, bool]:
    """持续排空子进程输出但只保留限额字节；超时回收本地 CLI 进程。

    Args:
        command: 内部构造的固定 Docker 命令，不经过 Shell。
        timeout: 此次 Docker CLI 的最大等待秒数。
        limit: 输出字节上限；超出部分丢弃但继续排空，避免管道死锁。
        cancel: 可选取消标记，供 MCP 取消请求终止 Docker 等待。
    Returns:
        退出码、保留字节、是否截断。
    Raises:
        subprocess.TimeoutExpired: CLI 超时；容器由 DockerRunner 的 finally 回收。
        BaseException: 取消或启动失败，原样传播。
    """
    # Docker CLI 需要系统环境，测试容器不继承此环境；宿主 API 凭据也不传给 CLI。
    env = {k: v for k, v in os.environ.items()
           if not any(x in k.upper() for x in ("KEY", "TOKEN", "SECRET", "PASSWORD"))}
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               shell=False, env=env)
    data, overflow = bytearray(), [False]

    def drain():
        """后台排空 Docker 日志，内存占用始终受 limit 限制。"""
        try:
            while chunk := process.stdout.read(4096):
                remaining = max(0, limit - len(data))
                data.extend(chunk[:remaining])
                overflow[0] |= len(chunk) > remaining
        except (OSError, ValueError):
            overflow[0] = True

    reader = Thread(target=drain, daemon=True)
    reader.start()
    try:
        deadline = monotonic() + timeout
        while True:
            if cancel is not None and cancel.is_set():
                raise KeyboardInterrupt()
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                process.wait(timeout=min(0.2, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
    except BaseException:
        process.kill()
        process.wait(timeout=5)
        raise
    finally:
        reader.join(timeout=2)
        if not reader.is_alive():
            process.stdout.close()
    return process.returncode, bytes(data), overflow[0] or reader.is_alive()


def parse_junit(archive: bytes, code: int, output: str) -> TestResult:
    """从 docker cp 的有界 tar 中读取唯一普通 JUnit 文件，不解包到宿主。

    Args:
        archive: 最多 4 MiB 的 Docker cp tar 字节。
        code: Docker start 返回的测试进程退出码。
        output: 已脱敏且截断的测试输出。
    Returns:
        与 V2 相同的 TestResult；缺失、损坏或无有效报告视为环境异常。
    """
    try:
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            members = tar.getmembers()
            if len(members) != 1 or not members[0].isfile() or members[0].name != "junit.xml":
                raise ValueError("无效报告归档")
            if members[0].size > 4_000_000:
                raise ValueError("报告过大")
            raw = tar.extractfile(members[0]).read(4_000_001)
    except (OSError, ValueError, tarfile.TarError, ET.ParseError):
        return TestResult(outcome="environment", returncode=code, output=output)
    return parse_junit_xml(raw, code, output)


def parse_junit_xml(raw: bytes, code: int, output: str) -> TestResult:
    """校验有界 JUnit XML 和退出码，不根据终端文本猜测通过数量。

    Args:
        raw: 从独立报告目录读取的 XML 字节，最大 4 MB。
        code: 容器进程的实际退出码。
        output: 已脱敏的 pytest 日志。
    Returns:
        兼容 V2 的测试结果；缺失、损坏或含实体声明的报告为环境异常。
    """
    passed = failed = skipped = 0
    try:
        if len(raw) > 4_000_000 or b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
            raise ValueError("拒绝超大 XML 或实体声明")
        for case in ET.fromstring(raw).iter("testcase"):
            if case.find("failure") is not None or case.find("error") is not None:
                failed += 1
            elif case.find("skipped") is not None:
                skipped += 1
            else:
                passed += 1
    except (ValueError, ET.ParseError):
        return TestResult(outcome="environment", returncode=code, output=output + "\nJUnit 报告缺失、损坏或不安全")
    if code in (2, 3, 4) or code not in (0, 1, 5):
        outcome = "environment"
    elif code == 5 or passed + failed == 0:
        outcome = "no_tests"
    elif code == 0 and passed > 0 and failed == 0:
        outcome = "passed"
    else:
        outcome = "failed"
    return TestResult(outcome=outcome, returncode=code, passed=passed, failed=failed,
                      skipped=skipped, output=output)


def read_report(directory: Path) -> bytes:
    """读取容器停止后的唯一报告文件，不跟随链接或读取特殊文件。

    Args:
        directory: 本轮独立宿主临时目录。
    Returns:
        最大 4 MB 的 junit.xml 原始字节。
    Raises:
        OSError: 报告不存在或无法打开。
        ValueError: 报告为链接、硬链接、特殊文件、超大文件或打开时身份变化。
    """
    path = directory / "junit.xml"
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 4_000_000:
        raise ValueError("拒绝非普通或超大报告")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("报告文件身份变化")
        raw = stream.read(4_000_001)
    if len(raw) > 4_000_000:
        raise ValueError("报告超过限额")
    return raw


class DockerRunner:
    """运行固定测试集，隔离文件写入和资源使用，不回退宿主执行。

    Attributes:
        workspace: 复用 V2 的可信工作区权限对象。
        tests: 启动时校验并固定的 pytest 路径白名单。
        image_id: 预检解析后的本地不可变镜像 ID。
    """

    def __init__(self, workspace, tests: list[str], image: str, *, timeout: float = 60,
                 task_id: str, artifacts: Path):
        """校验测试集与配置；此处不启动容器。

        Args:
            workspace: 已绑定的任务 worktree。
            tests: V2 测试路径格式，不接受 pytest 选项。
            image: 已预备的本地镜像名称或 ID。
            timeout: 每轮测试限时，1 到 300 秒。
            task_id: 用于容器归属标签的任务 ID。
            artifacts: 工作区外的日志目录。
        Raises:
            ValueError: 镜像名称、限时、任务标识或测试路径不合法。
        """
        if (not image or image.startswith("-") or any(c.isspace() for c in image)
                or not re.fullmatch(r"[a-f0-9]{32}", task_id)):
            raise ValueError("非法镜像或任务 ID")
        self.validator = PytestRunner(workspace, tests, timeout=timeout)
        self.workspace, self.tests = workspace, list(tests)
        self.image, self.timeout, self.task_id = image, timeout, task_id
        self.artifacts, self.image_id = Path(artifacts), ""
        self.cancel = Event()

    def preflight(self) -> str:
        """检查本地 Linux Docker 和镜像；不拉取镜像、不运行项目代码。

        Returns:
            后续固定使用的本地 image ID。
        Raises:
            RuntimeError: 服务、镜像或安全限制不满足。
        """
        host = os.environ.get("DOCKER_HOST", "")
        if host and not host.startswith(("npipe:", "unix:")):
            raise RuntimeError("只支持本地 Docker 服务")
        for command in (["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
                        ["docker", "info", "--format", "{{.OSType}}"],
                        ["docker", "image", "inspect", "--format", "{{.Id}}", self.image]):
            code, raw, truncated = capture(command, 15)
            if code or truncated:
                raise RuntimeError("Docker 或本地镜像不可用")
            value = raw.decode("utf-8", "replace").strip()
            if command[1] == "context" and not value.startswith(("npipe:", "unix:")):
                raise RuntimeError("只支持本地 Docker 服务")
            if command[1] == "info" and value != "linux":
                raise RuntimeError("只支持 Linux 容器")
            if command[1] == "image":
                if not re.fullmatch(r"sha256:[a-f0-9]{64}", value):
                    raise RuntimeError("无法确认镜像 ID")
                self.image_id = value
        code, raw, truncated = capture(["docker", "image", "inspect", "--format",
                                        '{{json (index .Config "Volumes")}}', self.image_id], 15)
        if code or truncated or raw.strip() not in {b"null", b"{}"}:
            raise RuntimeError("测试镜像不得声明额外数据卷")
        return self.image_id

    def snapshot(self, destination: Path) -> None:
        """只复制 worktree 中受跟踪的普通非敏感文件，隔离 .git 和宿主秘密。

        Args:
            destination: 本轮新建的临时目录。
        Raises:
            ValueError: 输入超过 1000 文件或 64 MiB，存在链接、硬链接或并发改动。
        """
        total = count = 0
        # 临时目录默认 0700；只读挂载后必须允许容器的非 root 用户读取过滤快照。
        destination.chmod(0o755)
        for name in git(self.workspace.root, "ls-files", "-z").split("\x00"):
            if not name or any(p.startswith(".") for p in name.split("/")):
                continue
            parts = safe_relative(name)
            source = self.workspace.path(name, source=False)
            stat = source.stat()
            if not source.is_file() or stat.st_nlink > 1:
                raise ValueError("容器输入必须为普通非硬链接文件")
            count, total = count + 1, total + stat.st_size
            if count > 1000 or total > 64 * 1024 * 1024:
                raise ValueError("容器输入超过限额")
            data = source.read_bytes()
            after = source.stat()
            # 读取会改变访问时间；仅比较与源码版本相关的身份、大小和修改时间。
            if ((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                    != (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
                    or len(data) != stat.st_size):
                raise ValueError("复制期间源码变化")
            target = destination.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            for parent in (target.parent, *target.parent.parents):
                if parent == destination:
                    break
                parent.chmod(0o755)
            target.write_bytes(data)
            target.chmod(0o644)

    def command(self, name: str, source: Path, reports: Path) -> list[str]:
        """构造固定安全参数；调用者不能追加挂载、环境变量或命令。

        Args:
            name: 内部随机容器名。
            source: 已过滤的临时输入目录。
            reports: 每轮独立的空临时报告目录，仅此目录可由容器写回。
        Returns:
            不经 Shell 的 docker create 参数列表。
        """
        if "," in str(source) or "," in str(reports):
            raise ValueError("快照路径不支持逗号")
        return ["docker", "create", "--name", name, "--label", "aicd.task=" + self.task_id,
                "--pull=never", "--network=none", "--read-only", "--user=65534:65534",
                "--cap-drop=ALL", "--security-opt=no-new-privileges=true",
                "--cpus=1", "--memory=1g", "--memory-swap=1g", "--pids-limit=128",
                "--ulimit", "nofile=256:256", "--ulimit", "core=0:0", "--ipc=none",
                "--log-driver=none", "--no-healthcheck", "--workdir=/workspace", "--entrypoint=python",
                "--env=PYTEST_DISABLE_PLUGIN_AUTOLOAD=1", "--env=PYTHONDONTWRITEBYTECODE=1",
                "--env=HOME=/tmp", "--env=PYTHONIOENCODING=utf-8",
                "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
                "--tmpfs", "/workspace:rw,noexec,nosuid,nodev,size=128m,mode=1777",
                "--mount", f"type=bind,source={source},target=/input,readonly",
                "--mount", f"type=bind,source={reports},target=/reports",
                self.image_id, "-I", "-c", _BOOTSTRAP, json.dumps(self.tests)]

    def _remove(self, name: str) -> None:
        """仅强制停止并移除具有本任务标签的随机容器，不清理其他容器。

        Raises:
            RuntimeError: 无法核对或移除容器，调用方必须报告环境阻塞。
        """
        code, raw, _ = capture(["docker", "inspect", "--format", '{{index .Config.Labels "aicd.task"}}', name], 10)
        if code or raw.decode("utf-8", "replace").strip() != self.task_id:
            raise RuntimeError("无法确认容器归属，保留现场")
        code, _, _ = capture(["docker", "rm", "-f", name], 10)
        if code:
            raise RuntimeError("容器清理失败，请人工检查任务容器")

    def cleanup_remaining(self) -> None:
        """在 MCP 进程退出后核对并回收本任务残留容器。

        Raises:
            RuntimeError: Docker 不可用、列表过大或容器归属不匹配。
        Notes:
            调用方必须先写入恢复操作的审计开始事件。只按完整任务标签查找，
            再逐个核对随机名称和归属；不使用全局 prune 或清理其他任务。
            用于覆盖 stdio 子进程提前退出、来不及执行 finally 的场景。
        """
        code, raw, cut = capture(["docker", "ps", "-a", "--filter", "label=aicd.task=" + self.task_id,
                                   "--format", "{{.Names}}"], 10)
        names = raw.decode("utf-8", "replace").splitlines()
        if code or cut or len(names) > 8 or any(not re.fullmatch(r"aicd-[a-f0-9]{32}", n) for n in names):
            raise RuntimeError("无法确认本任务残留容器，请人工检查任务标签")
        for name in names:
            self._remove(name)

    @timed("V3：Docker pytest 与容器回收")
    def run(self) -> TestResult:
        """执行一轮固定 pytest，所有退出路径都尝试回收任务容器。

        Returns:
            V2 TestResult；测试异常不会触发宿主 pytest。
        Notes:
            同步阻塞等待 Docker；输出由后台线程限额收集。超时先结束 CLI，
            再按任务标签移除实际容器。清理失败覆盖为环境错误，不能报告成功。
            JUnit 报告通过独立可写宿主临时目录传出；源码快照仍只读挂载。
            报告目录在容器回收和结果读取后清理，不依赖停止后失效的 tmpfs。
        """
        self.validator._validate_paths()
        if self.cancel.is_set():
            return TestResult(outcome="cancelled", output="测试开始前已取消")
        if not self.image_id:
            self.preflight()
        name = "aicd-" + uuid4().hex
        created, result = False, TestResult(outcome="environment")
        with tempfile.TemporaryDirectory(prefix="aicd-input-") as directory, tempfile.TemporaryDirectory(prefix="aicd-report-") as report_directory:
            source = Path(directory)
            reports = Path(report_directory)
            # Linux 上临时目录默认 0700；报告目录需允许容器 UID 65534 写入。
            # 此目录每轮新建且不包含源码、索引或用户文件，不能挂载任务产物根目录。
            reports.chmod(0o777)
            self.snapshot(source)
            command = self.command(name, source, reports)
            try:
                code, _, _ = capture(command, 20)
                if code:
                    raise RuntimeError("容器创建失败")
                created = True
                code, raw, truncated = capture(["docker", "start", "-a", name], self.timeout, cancel=self.cancel)
                sanitized = redact(raw.decode("utf-8", "replace")).encode("utf-8")
                output = sanitized[:16384].decode("utf-8", "ignore")
                truncated |= len(sanitized) > 16384
                log_path = self.artifacts / (name + ".log")
                log_path.write_text(output, encoding="utf-8")
                state_code, state_raw, state_cut = capture(["docker", "inspect", "--format", "{{json .State}}", name], 10)
                if state_code or state_cut:
                    raise RuntimeError("无法确认容器退出状态")
                state = json.loads(state_raw)
                if state.get("Running") or state.get("OOMKilled"):
                    raise RuntimeError("容器仍在运行或内存限额触发")
                code = state["ExitCode"]
                try:
                    raw_report = read_report(reports)
                except (OSError, ValueError):
                    raw_report = b""
                result = parse_junit_xml(raw_report, code, output)
                result.truncated, result.log_path = truncated, str(log_path)
            except subprocess.TimeoutExpired:
                result = TestResult(outcome="timeout", timed_out=True, output="Docker 测试或控制命令超时")
            except KeyboardInterrupt:
                result = TestResult(outcome="cancelled", output="Docker 测试已取消")
            except (OSError, RuntimeError, ValueError, KeyError):
                result = TestResult(outcome="environment", output="Docker 启动或报告收集失败")
            finally:
                # 即使 create 响应超时，也可能已产生容器；核对随机名与任务标签后回收。
                try:
                    self._remove(name)
                except (OSError, RuntimeError, subprocess.TimeoutExpired):
                    if created or result.outcome == "timeout":
                        result = TestResult(outcome="environment", output="容器清理未确认：" + name)
            result.command = ["docker", "pytest", *self.tests]
            if result.log_path is None:
                log_path = self.artifacts / (name + ".log")
                log_path.write_text(result.output, encoding="utf-8")
                result.log_path = str(log_path)
        return result
