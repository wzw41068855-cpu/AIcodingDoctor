"""仅在可信副本运行固定 pytest；本地 subprocess 不提供恶意代码隔离。"""

import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
from threading import Thread
import xml.etree.ElementTree as ET

from graph.editor import Workspace
from graph.state import TestResult
from utils.operation_timing import timed


def redact(text: str) -> str:
    """脱敏已知凭据环境值及常见赋值形式，保护报告和控制台。

    是一个日志脱敏器。它接收一段文本 text，返回一段“去敏感信息后的文本”。
    这个函数主要用于防止测试输出、模型输出或 diff 中把密钥直接打到控制台或报告里

    Args:
        text: 模型、测试或 Diff 产生的文本。
    Returns:
        替换敏感值后的文本，不输出环境变量本身。
    """
    for name, value in os.environ.items():
        if len(value) >= 6 and any(key in name.upper() for key in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
            text = text.replace(value, "[REDACTED]")
    return re.sub(r"(?i)((?:api[_-]?key|password|secret|token)\s*[=:]\s*)[^\s,;]+",
                  r"\1[REDACTED]", text)


def redact_data(value):
    """在 JSON 序列化前递归脱敏，避免正则替换破坏 JSON 引号。

    为什么需要它？
    因为 nodes.py 中会把任务信息序列化为 JSON，再发送给 LLM 或写入报告。先对结构化数据脱敏，可以避免直接替换 JSON 文本导致格式损坏。

    Args:
        value: 由字典、列表及标量组成的报告或模型证据。
    Returns:
        与输入结构相同、所有字符串值已脱敏的数据副本。
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: redact_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_data(item) for item in value]
    return value


class PytestRunner:
    """固定解释器、路径和资源上限的同步 pytest 执行器。
    这个类负责：
        1. 验证允许运行的测试路径。
        2. 启动 pytest 子进程。
        3. 限制测试执行时间。
        4. 收集并限制日志大小。
        5. 解析测试结果。
        6. 返回 TestResult。
    Attributes:
        tests: 启动时验证的路径白名单，不接受 LLM 提供的选项。
        timeout: pytest 进程最大等待秒数。
        output_limit: stdout/stderr 合并后的保留字节上限。
    """

    def __init__(self, workspace: Workspace, tests: list[str], *, timeout: float = 30,
                 output_limit: int = 16384):
        """验证固定测试目标与上限，不启动外部程序。

        Args:
            workspace: 经授权的可信副本。
            tests: 已有 tests/test 目录或 test_*.py 文件的相对路径。
            timeout: 0 到 300 秒之间，不含零。
            output_limit: 256 到 65536 的输出字节上限。
        Raises:
            ValueError: 白名单为空、路径不安全或资源上限非法。
        """
        #“早失败（fail fast）”。一开始就把高风险参数拦下来，例如测试路径数量、超时时间、输出上限都必须在安全范围内：
        if not tests or len(tests) > 8 or not 0 < timeout <= 300 or not 256 <= output_limit <= 65536:
            raise ValueError("测试路径及资源上限非法")
        self.workspace, self.tests = workspace, list(tests)
        self.timeout, self.output_limit = timeout, output_limit
        self._validate_paths()

    def _validate_paths(self):
        """每次执行前重新检查目标及其子树，防止路径被换成链接。

        负责路径白名单和目录安全检查。
        第一层规则是“目标类型必须受限”：目录只能是显式 tests/test，文件必须是 test_*.py。
        第二层规则是“目录树里不允许链接跳转”，它遍历目录并检查每个子路径是否是符号链接或 junction。
        """
        for relative in self.tests:
            target = self.workspace.path(relative, source=False)
            if (target.is_dir() and target.name not in {"tests", "test"}
                    or target.is_file() and not (target.name.startswith("test_") and target.suffix == ".py")):
                raise ValueError("测试必须位于显式 tests/test 目录或 test_*.py 文件")
            if target.is_dir():
                for directory, dirs, files in os.walk(target, followlinks=False):
                    for name in dirs + files:
                        path = Path(directory) / name
                        if path.is_symlink() or path.is_junction():
                            raise ValueError("测试目录不能包含链接")

    @staticmethod
    def _stop(process):
        """超时或取消时尽力终止进程树，最多等待五秒。

        _stop(process) 是一个跨平台的“尽力终止”逻辑，
        用于超时或取消后的回收。Windows 上使用 taskkill /T /F 杀进程树，
        POSIX 上使用 os.killpg 杀进程组。如果进程仍然存活，则调用 process.kill() 强制终止。

        Notes:
            Windows 使用固定 taskkill /T /F；POSIX 终止独立进程组。
            无法保证已脱离进程树的子孙进程被清理，V3 才提供容器隔离。
        """
        try:
            if os.name == "nt":
                subprocess.run([str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"),
                                "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, shell=False)
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            pass
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)

    @timed("测试：pytest 子进程与结果收集")
    def run(self) -> TestResult:
        """运行固定 pytest 并结合 JUnit 判断有效测试，捕获启动、超时和取消。

        graph/test_runner.py 里的 run() 是 PytestRunner 的核心方法：
        它在受控环境中启动 pytest，把执行输出限制在一定大小内，解析 JUnit 结果，
        然后返回结构化的 TestResult。它不是简单地“跑个命令”，而是把安全性、稳定性、
        可观测性都打包进一次测试运行流程。

        Returns:
            含退出码、有效通过/失败/跳过数和有限日志的结构化结果。
        Notes:
            阻塞等待外部进程；后台线程持续排空输出但只保存有限字节。
            JUnit 临时文件在退出时清理，避免重试读取上一轮的测试结果。
            禁用自动插件发现及继承的 pytest 参数，减少环境干扰。
        """
        try:#首先是“启动前校验 + 快速失败”。方法开头会再次检查测试路径是否合法（防止对象创建后路径被替换或目录状态变化）。
            self._validate_paths()
        except (OSError, ValueError):
            return TestResult(outcome="environment", output="测试路径校验失败")
        with tempfile.TemporaryDirectory(prefix="v2-pytest-") as temp:
            report = Path(temp) / "junit.xml"
            #在临时目录中构造本次运行的隔离文件（比如 junit.xml 和 pytest cache）。pytest 命令行被显式固定，并且强制覆盖部分配置，避免继承外部噪音参数：
            command = [sys.executable, "-m", "pytest", *self.tests, "-q", "-o", "addopts=",
                       "--override-ini=cache_dir=" + str(Path(temp) / "cache"),
                       "--junitxml=" + str(report)]
            env = os.environ.copy()
            #环境变量处理也很关键：先复制当前环境，再移除可能污染行为的变量（PYTEST_ADDOPTS、PYTEST_PLUGINS、PYTHONPATH），最后显式关闭插件自动加载：
            #这样做的目的，是尽量让测试结果不受本机用户环境影响，提升可重复性。
            for key in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONPATH"):
                env.pop(key, None)
            env.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
            captured = bytearray()
            overflow = [False]
            process = None
            try:
                #执行阶段通过 subprocess.Popen(...) 启动 pytest，
                process = subprocess.Popen(command, cwd=self.workspace.root, env=env, shell=False,
                                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           start_new_session=os.name != "nt",
                                           creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)

                def drain():
                    """持续排空管道防止死锁，丢弃超出上限的字节。"""
                    try:
                        while chunk := process.stdout.read(4096):
                            remaining = self.output_limit - len(captured)
                            captured.extend(chunk[:remaining])
                            overflow[0] |= len(chunk) > remaining
                    except (OSError, ValueError):
                        pass

                reader = Thread(target=drain, daemon=True)
                reader.start()
                outcome = None
                try:
                    #等待 pytest 进程结束，超时则捕获异常
                    process.wait(timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    self._stop(process)
                    outcome = "timeout"
                except KeyboardInterrupt:
                    self._stop(process)
                    outcome = "cancelled"
                reader.join(timeout=2)
                # 正常退出但仍有子进程持有 stdout 时，不无限等待管道 EOF。
                if reader.is_alive():
                    overflow[0] = True
                output = redact(bytes(captured).decode("utf-8", errors="replace"))
                output = output.encode("utf-8")[:self.output_limit].decode("utf-8", errors="ignore")
                passed = failed = skipped = 0
                #尝试从 junit.xml 统计 passed/failed/skipped，
                if report.exists() and report.stat().st_size <= 4_194_304:
                    try:
                        for case in ET.parse(report).iter("testcase"):
                            if case.find("failure") is not None or case.find("error") is not None:
                                failed += 1
                            elif case.find("skipped") is not None:
                                skipped += 1
                            else:
                                passed += 1
                    except ET.ParseError:
                        pass
                code = process.returncode
                if outcome is None:
                    if "No module named pytest" in output or code in (2, 3, 4):
                        outcome = "environment"
                    elif code == 5 or code == 0 and passed == 0:
                        outcome = "no_tests"
                    elif code == 0 and passed > 0 and failed == 0:
                        outcome = "passed"
                    else:
                        outcome = "failed"
                return TestResult(outcome=outcome, command=command, returncode=code,
                                  timed_out=outcome == "timeout", passed=passed, failed=failed,
                                  skipped=skipped, output=output, truncated=overflow[0])
            except (OSError, subprocess.SubprocessError):
                if process is not None and process.poll() is None:
                    self._stop(process)
                return TestResult(outcome="environment", command=command, output="pytest 启动或进程清理失败")
