"""使用虚拟时钟验证计时、异常传播及日志隔离，不访问外部 API。"""

import inspect

import pytest

from utils import operation_timing as timing


def test_elapsed_time_and_immediate_start(monkeypatch, tmp_path, capsys):
    """操作尚未结束时已输出开始行，终端和文件都记录精确耗时。"""
    ticks = iter([10.0, 12.345])
    monkeypatch.setattr(timing, "perf_counter", lambda: next(ticks))
    path = tmp_path / "logs" / "timing.log"
    with timing.timing_session(path):
        with timing.timed_operation("构建"):
            start = capsys.readouterr().out
            assert "[计时开始] 构建" in start and "计时结束" not in start
            assert "计时开始" in path.read_text(encoding="utf-8")
        end = capsys.readouterr().out
    assert "耗时=2.345s" in end and "完成" in end
    assert start.split("调用=")[1].strip() == end.split("调用=")[1].split(" |")[0]
    text = path.read_text(encoding="utf-8")
    assert "PID=" in text and "耗时=2.345s" in text


@pytest.mark.parametrize("error", [RuntimeError("private-key-and-source"), KeyboardInterrupt()])
def test_exception_and_cancel_preserved_without_details(error, capsys):
    """异常和取消原样传播，结束日志只暴露异常类型。"""
    with timing.timing_session():
        with pytest.raises(type(error)) as caught:
            with timing.timed_operation("调用"):
                raise error
    output = capsys.readouterr().out
    assert caught.value is error
    assert f"异常={type(error).__name__}" in output
    assert "耗时=" in output and "private-key-and-source" not in output


def test_decorator_preserves_result_signature_and_privacy(capsys):
    """装饰器不改变接口，且不会把参数或结果写入计时日志。"""
    def original(secret: str) -> str:
        """返回输入，供测试接口元信息是否保留。"""
        return secret

    wrapped = timing.timed("固定操作")(original)
    with timing.timing_session():
        assert wrapped("private-key-and-source") == "private-key-and-source"
    assert inspect.signature(wrapped) == inspect.signature(original)
    assert wrapped.__doc__ == original.__doc__
    assert "private-key-and-source" not in capsys.readouterr().out


def test_nested_session_restores_outer_sink(tmp_path, capsys):
    """嵌套会话退出后恢复外层日志目标，防止跨运行重复输出。"""
    outer = tmp_path / "outer.log"
    inner = tmp_path / "inner.log"
    with timing.timing_session(outer):
        with timing.timing_session(inner):
            with timing.timed_operation("内层"):
                pass
        with timing.timed_operation("外层"):
            pass
    assert "内层" not in outer.read_text(encoding="utf-8")
    assert "外层" not in inner.read_text(encoding="utf-8")
    assert capsys.readouterr().out.count("[计时开始]") == 2
    assert timing._sink.get() is None


def test_file_failure_keeps_console_and_result(tmp_path, capsys):
    """日志路径不可写时保留终端输出，不阻塞业务。"""
    obstacle = tmp_path / "file"
    obstacle.write_text("existing", encoding="utf-8")
    with timing.timing_session(obstacle / "log"):
        with timing.timed_operation("继续执行"):
            result = 42
    output = capsys.readouterr().out
    assert result == 42
    assert "文件日志不可写" in output and "计时结束" in output


def test_broken_sink_does_not_replace_business_error():
    """观测输出故障不能覆盖原始业务异常。"""
    def broken(message):
        """模拟日志输出失败。"""
        raise OSError("log failure")

    token = timing._sink.set(broken)
    try:
        with pytest.raises(ValueError, match="business"):
            with timing.timed_operation("操作"):
                raise ValueError("business")
    finally:
        timing._sink.reset(token)
