"""项目经验管理和运行中反馈 API；复用应用的本地令牌中间件。"""

from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from memory.artifacts import Artifacts
from memory.experience import ExperienceStore
from web_runtime.store import Store


class ExperienceInput(BaseModel):
    """用户明确确认后的稳定项目经验。"""
    repository: str
    content: str = Field(min_length=1, max_length=2000)
    source: str = Field(default="用户显式保存", max_length=500)
    explicit_confirm: bool = False
    experience_id: str | None = None


class FeedbackInput(BaseModel):
    """仅在安全阶段边界接收的用户补充。"""
    feedback: str = Field(min_length=1, max_length=2000)


def register_memory_routes(app: FastAPI, root: Path, store: Store) -> None:
    """将小型记忆 API 与主路由解耦。

    Args:
        app: 已安装本地边界中间件的应用。
        root: 宿主存储目录。
        store: 任务状态存储。
    """
    experiences = ExperienceStore(root / "experiences.sqlite")

    @app.get("/api/experiences")
    def listing(repository: str) -> list[dict]:
        """查看当前项目经验。"""
        return experiences.listing(repository)

    @app.post("/api/experiences")
    def save(body: ExperienceInput) -> dict:
        """显式保存或更新，不接受 Agent 自动调用。"""
        try:
            identity = experiences.save(body.repository, body.content, body.source,
                explicit=body.explicit_confirm, experience_id=body.experience_id)
            return {"id": identity}
        except (ValueError, KeyError):
            raise HTTPException(422, "需要显式确认、无凭据内容和当前项目有效条目")

    @app.delete("/api/experiences/{identity}")
    def delete(identity: str, repository: str) -> dict:
        """删除当前项目经验，其他项目不可见。"""
        if not experiences.delete(repository, identity):
            raise HTTPException(404, "经验不存在")
        return {"deleted": True}

    @app.post("/api/tasks/{task_id}/feedback")
    def feedback(task_id: str, body: FeedbackInput) -> dict:
        """运行中反馈在下一个 Supervisor 边界读取，不能直接触发副作用。"""
        try:
            if not store.enqueue_feedback(task_id, {"id": uuid4().hex, "text": body.feedback}):
                raise HTTPException(409, "仅运行中任务接收补充；待审批请拒绝并填写调整意见")
        except KeyError:
            raise HTTPException(404, "任务不存在")
        return {"accepted": True, "effective_at": "下一个阶段边界"}

    @app.get("/api/tasks/{task_id}/artifacts/{key}")
    def artifact(task_id: str, key: str, offset: int = 0) -> dict:
        """通过内容引用读取有界工具结果，不接受磁盘路径。"""
        try:
            store.get(task_id)
            return Artifacts(root / task_id).read(key, offset)
        except (KeyError, OSError, ValueError):
            raise HTTPException(404, "任务产物不存在或引用无效")
