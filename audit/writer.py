"""使用持久化开始/结束事件约束工具副作用，不保存业务参数正文。"""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from threading import RLock
from time import perf_counter
from uuid import uuid4


class AuditError(RuntimeError):
    """审计不可持久化；调用方必须阻塞任务并保留已有修改。"""


class AuditWriter:
    """为单任务串行工具执行保存追加式 JSONL 事件。

    audit/writer.py 的核心思路是：工具有副作用前必须先落盘审计事件，否则不允许继续执行。

    Attributes:
        path: 位于工作树之外的审计文件。
        task_id: 固定任务标识，不由模型提供。
        blocked: 审计故障后锁死后续操作，不能靠重试绕过。

    Notes:
        每次写入 flush/fsync；同进程使用锁。运行入口必须保证父进程与
        MCP 子进程不同时写此文件。本日志不是防宿主篡改的密码学存证。
    """

    def __init__(self, path: Path, task_id: str):
        """绑定已有产物目录中的日志路径，不主动创建业务资源。

        Args:
            path: 由任务管理器生成的日志位置。
            task_id: 本次任务的固定 ID。
        """
        self.path, self.task_id = Path(path), task_id
        self.blocked = False
        self._lock = RLock()#线程锁，一个进程只有一个 AuditWriter 实例

    def append(self, event: dict) -> None:
        """持久化白名单事件字段，失败后关闭本实例的执行权限。

        Args:
            event: 内部构造的元信息；禁止放入 Prompt、源码、异常正文和密钥。
        Raises:
            AuditError: 文件不可写或实例此前已失败。
        """
        with self._lock:#先拿 RLock 保证同进程串行，
            if self.blocked:#如果之前已经失败过，就直接 raise AuditError
                raise AuditError("审计已失效，任务阻塞并保留现场")
            try:
                record = dict(event, task_id=self.task_id,
                              time=datetime.now(timezone.utc).isoformat())
                with self.path.open("a", encoding="utf-8") as stream:
                    #写入是 JSONL（每行一个 JSON）：
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                    #flush + fsync 很关键：不仅写到 Python 缓冲区，还尽量落到 OS 层持久存储，提高“写成功即可信”的概率。
                    stream.flush()
                    os.fsync(stream.fileno())
            except (OSError, ValueError, TypeError) as exc:
                self.blocked = True#如果写入失败，标记 blocked=True，后续 append 都会 raise AuditError
                raise AuditError("审计写入失败，任务阻塞并保留现场") from exc

    @contextmanager
    def operation(self, tool: str, node: str = "runtime"):
        """先持久化开始事件，再允许操作；结束失败不回滚或重复操作。

        Args:
            tool: 开发者注册的工具名称。
            node: 受限节点名称，仅用于观测而非授权。
        Yields:
            可补充文件哈希、退出码等安全字段的结束事件字典。
        Raises:
            AuditError: 操作前写入失败则不进入业务；操作后失败保留副作用。
            BaseException: 原样传播业务异常和取消，正文不写入日志。
        """
        with self._lock:
            call_id = uuid4().hex
            #append 先写入开始事件，确保副作用前有持久化记录；失败则 raise AuditError
            self.append({"phase": "start", "tool": tool, "node": node, "call_id": call_id})
            started = perf_counter()#记录开始时间
            # 允许业务操作，异常会原样传播；结束事件必须在 finally 中
            # 以保证即使异常也能记录结束事件，避免审计日志不完整。
            # 结束事件不回滚副作用，避免重复执行工具。
            result = {"status": "completed"}
            try:
                yield result
            except BaseException as exc:#异常路径：
                result.update(status="error", error_type=type(exc).__name__)
                raise#raise 原样抛出，不吞业务异常。
            finally:#最后无论成功失败都执行 finally，写结束事件：
                self.append(dict(result, phase="end", tool=tool, node=node, call_id=call_id,
                                 duration_seconds=round(perf_counter() - started, 6)))
