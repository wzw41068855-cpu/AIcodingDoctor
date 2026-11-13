"""pytest 子进程结果分类与资源限制测试，不执行不可信代码。"""

import subprocess

import pytest

from graph.editor import Workspace
from graph.test_runner import PytestRunner, redact, redact_data


@pytest.fixture
def runner(tmp_path):
    """固定白名单目录，每个测试可写入可信测试源码。"""
    (tmp_path / "tests").mkdir()
    return PytestRunner(Workspace(tmp_path, trusted_copy=True), ["tests"], timeout=10, output_limit=512)


@pytest.mark.parametrize("source,outcome,passed,failed,skipped", [
    ("def test_ok(): assert True\n", "passed", 1, 0, 0),
    ("def test_bad(): assert False\n", "failed", 0, 1, 0),
    ("", "no_tests", 0, 0, 0),
    ("import pytest\n@pytest.mark.skip\ndef test_skip(): pass\n", "no_tests", 0, 0, 1),
    ("import missing_v2_dependency\n", "environment", 0, 1, 0),
])
def test_real_pytest_classification(runner, source, outcome, passed, failed, skipped):
    """真实 subprocess 覆盖通过、失败、无测试、全跳过与导入环境错误。"""
    (runner.workspace.root / "tests" / "test_sample.py").write_text(source, encoding="utf-8")
    result = runner.run()
    assert result.outcome == outcome
    assert (result.passed, result.failed, result.skipped) == (passed, failed, skipped)
    assert result.command[:3] == [result.command[0], "-m", "pytest"]
    assert len(result.output.encode()) <= runner.output_limit


def test_timeout_and_output_limit(runner):
    """真实超时须终止进程；大量输出只保留配置字节数。"""
    file = runner.workspace.root / "tests" / "test_sample.py"
    file.write_text("import time\ndef test_slow(): time.sleep(30)\n", encoding="utf-8")
    runner.timeout = 0.3
    assert runner.run().outcome == "timeout"
    runner.timeout = 10
    file.write_text("def test_noisy():\n    print('x' * 100000)\n    assert False\n", encoding="utf-8")
    result = runner.run()
    assert result.outcome == "failed" and result.truncated
    assert len(result.output.encode()) <= 512


def test_launch_failure_and_missing_pytest(runner, monkeypatch):
    """启动异常及解释器缺少 pytest 都产生 environment。"""
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: (_ for _ in ()).throw(OSError()))
    assert runner.run().outcome == "environment"


def test_missing_pytest_in_interpreter(runner, monkeypatch):
    """模拟解释器没有安装 pytest，不能把退出码一误当作普通断言失败。"""
    import io
    from types import SimpleNamespace
    process = SimpleNamespace(stdout=io.BytesIO(b"No module named pytest\n"), returncode=1,
                              wait=lambda **kw: 1, poll=lambda: 1)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: process)
    assert runner.run().outcome == "environment"


@pytest.mark.parametrize("tests", [[], ["../tests"], ["--help"], ["."], ["other"]])
def test_invalid_test_paths(runner, tests):
    """模型或用户不能借测试路径注入 pytest 开关或父目录。"""
    with pytest.raises((OSError, ValueError)):
        PytestRunner(runner.workspace, tests)


def test_log_redaction(monkeypatch):
    """环境凭据值和常见敏感赋值不出现在输出。"""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "v2-test-secret-value")
    assert "v2-test-secret-value" not in redact("failure v2-test-secret-value")
    assert "123456" not in redact("password=123456")
    import json
    report = redact_data({"request": 'password=123456 "quoted"', "items": ["token=abc123"]})
    assert json.loads(json.dumps(report)) == report
    assert "123456" not in report["request"] and "abc123" not in report["items"][0]
