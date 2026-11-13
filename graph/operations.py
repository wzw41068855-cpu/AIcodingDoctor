"""副作用意图账本：未知结果禁止重放，已知结果可复用。"""

import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Callable, Any


def digest(value: Any) -> str:
    """计算稳定输入标识。

    Args:
        value: 可 JSON 序列化输入。
    Returns:
        SHA256 摘要。
    """
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class OperationJournal:
    """在副作用前提交意图，避免崩溃恢复重复执行。"""

    def __init__(self, path: Path) -> None:
        """打开任务独占账本。

        Args:
            path: 工作树外的数据库文件。
        """
        self.path = str(path)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS operations "
                       "(id TEXT PRIMARY KEY, input_hash TEXT, status TEXT, result TEXT)")

    def run(self, operation_id: str, inputs: Any, action: Callable[[], Any],
            reconcile: Callable[[], Any] | None = None) -> Any:
        """复用完成结果，或先对账再决定是否可以恢复。

        Args:
            operation_id: 宿主生成的稳定操作 ID。
            inputs: 绑定审批和执行输入的摘要依据。
            action: 首次执行的受控操作。
            reconcile: 仅能确认已完成结果，不能重新执行副作用。
        Returns:
            已确认的操作结果。
        Raises:
            RuntimeError: 输入冲突或上次结果不确定。
        """
        fingerprint = digest(inputs)
        with sqlite3.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT input_hash,status,result FROM operations WHERE id=?",
                             (operation_id,)).fetchone()
            if row and row[0] != fingerprint:
                raise RuntimeError("操作 ID 输入已变化，禁止重放")
            if row and row[1] == "done":
                if reconcile is not None and reconcile() is None:
                    raise RuntimeError("已完成操作的目标已变化，需重新审查")
                return json.loads(row[2])
            if not row:
                db.execute("INSERT INTO operations VALUES (?,?,'pending',NULL)",
                           (operation_id, fingerprint))
        # 意图事务已经提交，不能把写文件包在未提交的 SQLite 事务内。
        result = reconcile() if row and reconcile else None
        if row and result is None:
            raise RuntimeError("上次操作结果未确认，需人工对账；禁止自动重放")
        if not row:
            result = action()
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE operations SET status='done',result=? WHERE id=?",
                       (json.dumps(result, ensure_ascii=False), operation_id))
        return result
