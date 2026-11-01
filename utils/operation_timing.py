"""同步操作的轻量计时日志，不记录调用参数、源码或异常正文。"""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import logging
from pathlib import Path
import sys
from time import perf_counter
from uuid import uuid4

from utils.logger import get_logger


_sink = ContextVar("operation_timing_sink", default=None)
_logger = get_logger("timing")


def _emit(message: str) -> None:
    """输出固定格式的计时信息，日志故障不能覆盖业务结果或原始异常。"""
    try:
        sink = _sink.get()
        (sink if sink is not None else _logger.info)(message)
    except Exception:
        # 观测能力失效不能让已经写入的补丁被误判为业务操作失败。
        pass


@contextmanager
def timing_session(log_path: Path | None = None):
    """为当前调用上下文启用带时间戳的终端及可选文件日志。

    Args:
        log_path: UTF-8 追加日志路径；None 仅输出终端。目录不存在时创建。

    Yields:
        None。作用域退出时恢复外层输出目标并关闭本次创建的 Handler。

    Notes:
        独立 Logger 不受 CLI 对第三方日志的静默设置影响，也不修改旧 Agent
        的日志配置。文件不可写时退化为终端输出；每条日志立即刷新。
        ContextVar 隔离嵌套调用上下文，不会自动传入新的进程或裸线程。
    """
    logger = logging.Logger("v2_timing", level=logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    logger.addHandler(console)
    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            logger.addHandler(logging.FileHandler(log_path, encoding="utf-8"))
        except OSError:
            console.setFormatter(logging.Formatter("%(message)s"))
            logger.warning("[计时] 文件日志不可写，仅输出终端")
    for handler in logger.handlers:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s.%(msecs)03d | PID=%(process)d | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
    token = _sink.set(logger.info)
    try:
        yield
    finally:
        _sink.reset(token)
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()


@contextmanager
def timed_operation(operation: str):
    """记录同步操作的开始、结束及包含阻塞等待的真实耗时。

    Args:
        operation: 开发者定义的静态操作名；不得拼入请求、路径、凭据或源码。

    Yields:
        None。业务返回值、异常和取消语义保持不变。

    Raises:
        BaseException: 原样传播被包围操作的异常，包括 KeyboardInterrupt。

    Notes:
        时间戳来自日志系统，耗时使用单调的 perf_counter，避免系统校时干扰。
        嵌套范围包含子操作时间，不能直接相加。完成只表示正常返回，
        不表示测试通过或业务成功；SDK 内部重试包含在本次调用耗时内。
    """
    call_id = uuid4().hex[:12]
    started = perf_counter()
    _emit(f"[计时开始] {operation} | 调用={call_id}")
    outcome = "完成"
    try:
        yield
    except BaseException as exc:
        # 只保留异常类型，避免 SDK 异常正文携带凭据或请求内容。
        outcome = f"异常={type(exc).__name__}"
        raise
    finally:
        elapsed = perf_counter() - started
        _emit(f"[计时结束] {operation} | 调用={call_id} | {outcome} | 耗时={elapsed:.3f}s")


def timed(operation: str):
    """装饰同步函数，在保持签名、Docstring 和返回值的前提下输出计时。

    Args:
        operation: 固定的操作名，遵循 timed_operation 的敏感信息限制。

    Returns:
        同步函数装饰器；不适用于协程函数或延迟执行的生成器。
    """
    def decorate(function):
        """绑定固定标签，避免检查或序列化业务参数。"""
        @wraps(function)
        def wrapped(*args, **kwargs):
            """执行原函数，并在返回或抛出异常时结束计时。"""
            with timed_operation(operation):
                return function(*args, **kwargs)
        return wrapped
    return decorate
