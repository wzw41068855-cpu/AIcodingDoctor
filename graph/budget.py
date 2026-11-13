"""独立于 Graph checkpoint 的持久预算，恢复不清零。"""

import json
from pathlib import Path
import sqlite3
import time
from typing import Any

from pydantic import BaseModel, Field
import config


class Limits(BaseModel):
    """任务硬上限；无真实 Token 时依靠调用和耗时上限。"""

    model_calls: int = Field(default=config.AGENT_MAX_MODEL_CALLS, gt=0)
    tool_calls: int = Field(default=config.AGENT_MAX_TOOL_CALLS, gt=0)
    total_tokens: int = Field(default=config.AGENT_MAX_TOTAL_TOKENS, gt=0)
    seconds: float = Field(default=config.AGENT_MAX_SECONDS, gt=0)
    stage_steps: int = Field(default=config.AGENT_MAX_STAGE_STEPS, gt=0)
    no_progress: int = Field(default=config.AGENT_MAX_NO_PROGRESS, gt=0)


class BudgetExceeded(RuntimeError):
    """已到硬上限，禁止后续模型和工具调用。"""


class Budget:
    """先预留调用次数再执行，SQLite 保证重启后不低估。"""

    def __init__(self, path: Path, limits: Limits | None = None) -> None:
        """读取已冻结的配置，禁止恢复时扩大预算。

        Args:
            path: 任务独立数据库。
            limits: 首次创建时的限制。
        """
        self.path = str(path)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS budget (id INTEGER PRIMARY KEY, data TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS reservations (id TEXT PRIMARY KEY)")
            data = {"limits": (limits or Limits()).model_dump(), "model_calls": 0,
                    "tool_calls": 0, "total_tokens": 0, "unknown_token_calls": 0,
                    "started": time.time(), "stop_reason": ""}
            db.execute("INSERT OR IGNORE INTO budget VALUES (1,?)", (json.dumps(data),))

    def snapshot(self) -> dict[str, Any]:
        """读取累计值和冻结配置。

        Returns:
            可展示的预算状态。
        """
        with sqlite3.connect(self.path) as db:
            data = json.loads(db.execute("SELECT data FROM budget WHERE id=1").fetchone()[0])
        data["elapsed_seconds"] = time.time() - data["started"]
        return data

    def reserve(self, kind: str, operation_id: str) -> None:
        """调用前持久预留，恢复同一工具操作不重复计数。

        Args:
            kind: model_calls 或 tool_calls。
            operation_id: 宿主生成的操作标识。
        Raises:
            BudgetExceeded: 已用完任一硬预算。
        """
        with sqlite3.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            data = json.loads(db.execute("SELECT data FROM budget WHERE id=1").fetchone()[0])
            limits = data["limits"]
            reason = data["stop_reason"]
            if time.time() - data["started"] >= limits["seconds"]:
                reason = "seconds"
            if data["total_tokens"] >= limits["total_tokens"]:
                reason = "total_tokens"
            existing = db.execute("SELECT 1 FROM reservations WHERE id=?", (operation_id,)).fetchone()
            if not existing and data[kind] >= limits[kind]:
                reason = kind
            if reason:
                data["stop_reason"] = reason
            elif not existing:
                data[kind] += 1
                db.execute("INSERT INTO reservations VALUES (?)", (operation_id,))
            db.execute("UPDATE budget SET data=? WHERE id=1", (json.dumps(data),))
        if reason:
            raise BudgetExceeded("预算耗尽：" + reason)

    def record_tokens(self, tokens: int | None) -> None:
        """累计真实 Token；缺失时单独记录未知次数。

        Args:
            tokens: SDK 返回的实际用量，未知为 None。
        """
        with sqlite3.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            data = json.loads(db.execute("SELECT data FROM budget WHERE id=1").fetchone()[0])
            data["total_tokens"] += tokens or 0
            data["unknown_token_calls"] += int(tokens is None)
            db.execute("UPDATE budget SET data=? WHERE id=1", (json.dumps(data),))
