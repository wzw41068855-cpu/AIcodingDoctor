"""Docker 命令、输入过滤、结果分类及显式启用的真实容器测试。"""

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
from threading import Event
from uuid import uuid4

import pytest

from graph.editor import Workspace
from sandbox import docker_runner as docker


def archive(xml: bytes) -> bytes:
    """构造 docker cp 格式的内存 tar，不在宿主解包。"""
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as tar:
        info = tarfile.TarInfo("junit.xml")
        info.size = len(xml)
        tar.addfile(info, io.BytesIO(xml))
    return stream.getvalue()


@pytest.mark.parametrize("xml,code,outcome,passed,failed", [
    (b"<testsuite><testcase/></testsuite>", 0, "passed", 1, 0),
    (b"<testsuite><testcase><failure/></testcase></testsuite>", 1, "failed", 0, 1),
    (b"<testsuite/>", 5, "no_tests", 0, 0),
    (b"<testsuite><testcase><error/></testcase></testsuite>", 2, "environment", 0, 1),
    (b"broken", 0, "environment", 0, 0),
    (b'<!DOCTYPE x [<!ENTITY a "b">]><testsuite/>', 0, "environment", 0, 0),
])
def test_junit(xml, code, outcome, passed, failed):
    """报告必须与退出码一起验证，缺失或非法 XML 不能假装通过。"""
    result = docker.parse_junit(archive(xml), code, "output")
    assert (result.outcome, result.passed, result.failed) == (outcome, passed, failed)


def test_report_file_validation(tmp_path):
    """报告必须存在、是普通单链接文件且不超限；终端显示通过不能替代报告。"""
    with pytest.raises(OSError):
        docker.read_report(tmp_path)
    report = tmp_path / "junit.xml"
    report.write_bytes(b"<testsuite><testcase/></testsuite>")
    assert docker.parse_junit_xml(docker.read_report(tmp_path), 0, "1 passed").passed == 1
    report.write_bytes(b"broken")
    assert docker.parse_junit_xml(docker.read_report(tmp_path), 0, "1 passed").outcome == "environment"
    report.write_bytes(b"x" * 4_000_001)
    with pytest.raises(ValueError):
        docker.read_report(tmp_path)
    report.unlink()
    report.mkdir()
    with pytest.raises(ValueError):
        docker.read_report(tmp_path)


def test_report_hardlink_rejected(tmp_path):
    """容器输出不能用硬链接借用其他文件作为测试报告。"""
    target = tmp_path / "other.xml"
    target.write_bytes(b"<testsuite/>")
    os.link(target, tmp_path / "junit.xml")
    with pytest.raises(ValueError):
        docker.read_report(tmp_path)


def runner(task):
    """绑定真实工作树和固定测试集，不在初始化时启动 Docker。"""
    _, manager, config = task
    return docker.DockerRunner(Workspace(manager.root, trusted_copy=True), config.tests, config.image,
        timeout=5, task_id=config.task_id, artifacts=manager.artifacts)


def test_secure_command_and_filtered_snapshot(task, tmp_path):
    """安全限制、固定入口和只读输入不可由工具参数覆盖。"""
    run = runner(task)
    run.image_id = "sha256:" + "a" * 64
    target = tmp_path / "snapshot"
    target.mkdir()
    (run.workspace.root / ".env").write_text("not-to-copy", encoding="utf-8")
    run.snapshot(target)
    assert not (target / ".git").exists() and not (target / ".env").exists()
    assert (target / "calculator.py").exists()
    reports = tmp_path / "reports"
    reports.mkdir()
    command = run.command("fixture", target, reports)
    for flag in ["--network=none", "--read-only", "--user=65534:65534", "--cap-drop=ALL",
                 "--security-opt=no-new-privileges=true", "--cpus=1", "--memory=1g",
                 "--memory-swap=1g", "--pids-limit=128", "--pull=never"]:
        assert flag in command
    assert "readonly" in command[command.index("--mount") + 1]
    assert f"type=bind,source={reports},target=/reports" in command
    assert not any("DASHSCOPE" in c or "docker.sock" in c for c in command)


def test_run_and_timeout_cleanup(task, monkeypatch):
    """测试通过和超时都清理本任务容器，不启动宿主 pytest。"""
    run = runner(task)
    run.image_id = "sha256:" + "a" * 64
    calls, timed_out, report_paths = [], [False], []

    def fake(command, timeout, limit=16384, cancel=None):
        """模拟 Docker 边界，固定报告不包含真实外部依赖。"""
        calls.append(command)
        if command[1] == "create":
            mount = next(value for value in command if value.endswith("target=/reports"))
            report_paths.append(Path(mount.removeprefix("type=bind,source=").removesuffix(",target=/reports")))
        if command[1] == "start":
            if timed_out[0]:
                raise subprocess.TimeoutExpired(command, timeout)
            (report_paths[-1] / "junit.xml").write_bytes(b"<testsuite><testcase/></testsuite>")
            return 0, b"1 passed", False
        if command[1] == "inspect":
            if "{{json .State}}" in command:
                return 0, b'{"Running": false, "OOMKilled": false, "ExitCode": 0}', False
            return 0, run.task_id.encode(), False
        return 0, b"", False

    monkeypatch.setattr(docker, "capture", fake)
    assert run.run().outcome == "passed"
    assert not report_paths[-1].exists()
    assert not any(c[1] == "cp" for c in calls)
    assert any(c[1:3] == ["rm", "-f"] for c in calls)
    timed_out[0] = True
    calls.clear()
    result = run.run()
    assert result.outcome == "timeout" and result.timed_out
    assert not report_paths[-1].exists()
    assert any(c[1:3] == ["rm", "-f"] for c in calls)


def test_preflight_never_pulls_or_accepts_remote_daemon(task, monkeypatch):
    """缺少本地服务或远程 Docker 都阻塞，不触发 pull/build 或宿主执行。"""
    run = runner(task)
    calls = []

    def remote(command, timeout, limit=16384):
        """模拟远程上下文，不连接任何实际 Docker。"""
        calls.append(command)
        return 0, b"ssh://remote", False

    monkeypatch.setattr(docker, "capture", remote)
    with pytest.raises(RuntimeError, match="本地"):
        run.preflight()
    assert len(calls) == 1 and not run.image_id


def test_capture_bounds_output_and_kills_on_cancel():
    """使用固定可信子进程验证输出限额、超时及取消，不执行仓库代码。"""
    code, output, cut = docker.capture([sys.executable, "-c", "print('x' * 100000)"], 10, 1024)
    assert code == 0 and len(output) == 1024 and cut
    with pytest.raises(subprocess.TimeoutExpired):
        docker.capture([sys.executable, "-c", "import time; time.sleep(30)"], 0.1)
    cancel = Event()
    cancel.set()
    with pytest.raises(KeyboardInterrupt):
        docker.capture([sys.executable, "-c", "import time; time.sleep(30)"], 10, cancel=cancel)


def test_already_cancelled_never_starts_container(task, monkeypatch):
    """取消后不能再创建容器，保持固定任务的取消语义。"""
    run = runner(task)
    run.cancel.set()
    monkeypatch.setattr(docker, "capture", lambda *a, **kw: pytest.fail("不应调用 Docker"))
    assert run.run().outcome == "cancelled"


def test_recovery_only_removes_matching_task_containers(task, monkeypatch):
    """服务退出后仅按完整任务标签恢复清理，每次删除前再次核对归属。"""
    run = runner(task)
    name, calls = "aicd-" + "a" * 32, []

    def fake(command, timeout, limit=16384, cancel=None):
        """只模拟本任务残留容器，不连接 Docker。"""
        calls.append(command)
        if command[1] == "ps":
            assert "label=aicd.task=" + run.task_id in command
            return 0, name.encode(), False
        if command[1] == "inspect":
            return 0, run.task_id.encode(), False
        return 0, b"", False

    monkeypatch.setattr(docker, "capture", fake)
    run.cleanup_remaining()
    assert [c[1] for c in calls] == ["ps", "inspect", "rm"]
    assert calls[-1] == ["docker", "rm", "-f", name]



@pytest.mark.skipif(not os.getenv("AICD_V3_SECURITY_IMAGE"), reason="未设置仅用于创建检查的本地镜像 AICD_V3_SECURITY_IMAGE")
def test_real_docker_security_configuration(task, tmp_path):
    """实际创建但不启动容器，检查安全限制；无需镜像内具有 Python 或 pytest。"""
    run = runner(task)
    run.image = os.environ["AICD_V3_SECURITY_IMAGE"]
    run.preflight()
    source = tmp_path / "input"
    source.mkdir()
    run.snapshot(source)
    name = "aicd-" + uuid4().hex
    try:
        reports = tmp_path / "reports"
        reports.mkdir()
        code, output, _ = docker.capture(run.command(name, source, reports), 20)
        assert code == 0, output
        code, raw, _ = docker.capture(["docker", "inspect", "--format", "{{json .HostConfig}}", name], 10)
        assert code == 0
        settings = json.loads(raw)
        assert settings["NetworkMode"] == "none" and settings["ReadonlyRootfs"]
        assert not settings["Privileged"] and settings["CapDrop"] == ["ALL"]
        assert settings["Memory"] == settings["MemorySwap"] == 1073741824
        assert settings["NanoCpus"] == 1000000000 and settings["PidsLimit"] == 128
        assert "no-new-privileges=true" in settings["SecurityOpt"]
        code, user, _ = docker.capture(["docker", "inspect", "--format", "{{.Config.User}}", name], 10)
        assert code == 0 and user.strip() == b"65534:65534"
        code, mounts, _ = docker.capture(["docker", "inspect", "--format", "{{json .Mounts}}", name], 10)
        assert code == 0
        binds = [m for m in json.loads(mounts) if m["Type"] == "bind"]
        mounts_by_path = {m["Destination"]: m for m in binds}
        assert len(binds) == 2
        assert not mounts_by_path["/input"]["RW"]
        assert mounts_by_path["/reports"]["RW"]
        code, running, _ = docker.capture(["docker", "inspect", "--format", "{{.State.Running}}", name], 10)
        assert code == 0 and running.strip() == b"false"
    finally:
        run._remove(name)
