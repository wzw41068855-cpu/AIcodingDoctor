"""只绑定本机的 FastAPI、任务调度及持久 SSE。"""

import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
from threading import Lock
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from web_runtime.store import Store


class TaskInput(BaseModel):
    """用户固定授权参数；密钥仅转交子进程环境，不写 SQLite。"""
    model_config = ConfigDict(extra="forbid")
    repository: str
    request: str = Field(min_length=1, max_length=4000)
    mode: str = Field(default="repair", pattern="^(repair|ask)$")
    trusted_copy: bool
    allowed_files: list[str] = Field(min_length=1, max_length=8)
    tests: list[str] = Field(default_factory=lambda: ["tests"], min_length=1, max_length=8)
    image: str = Field(default="python:3.13", min_length=1)
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    max_tokens: int = Field(default=4096, ge=256, le=16384)
    context_chars: int = Field(default=24000, ge=4000, le=24000)
    max_iterations: int = Field(default=3, ge=1, le=5)
    timeout: float = Field(default=60, gt=0, le=300)


class Decision(BaseModel):
    """审批绑定中断 ID，避免旧页面批准新一轮操作。"""
    approval_id: str
    approve: bool
    feedback: str = Field(default="", max_length=2000)
    api_key: str = ""


class ApplyDecision(BaseModel):
    """应用修改前的明确确认，不接受客户端指定写入路径。"""
    confirm: bool = False


def create_app(storage=None):
    """创建本地应用；目录位于源码仓库之外，满足 worktree 隔离约束。

    Args:
        storage: 测试或本机指定的持久目录；默认使用用户临时目录固定子目录。
    Returns:
        FastAPI 应用；单进程调度，不支持多个 Uvicorn worker。
    """
    root = Path(storage or Path(tempfile.gettempdir()) / "aicd-v4").resolve()
    #创建一个临时目录用于存储任务数据，确保与源码仓库隔离。
    root.mkdir(parents=True, exist_ok=True)

    #store是一个持久化存储对象，用于记录任务的状态和数据；processes是一个字典，用于跟踪正在运行的任务进程。
    store, processes = Store(root / "tasks.sqlite"), {}

    #生成一个随机的本地令牌，用于验证请求来源，防止未经授权的访问。
    token = secrets.token_urlsafe(32)

    #创建一个线程锁，用于在多线程环境中保护对共享资源的访问，确保任务调度和状态更新的线程安全。
    application_lock = Lock()

    async def monitor(task_id, process):
        """后台等待进程退出；未到持久等待点的异常退出禁止自动重放。"""
        await asyncio.to_thread(process.wait)
        processes.pop(task_id, None)
        if store.get(task_id)["status"] == "running":
            store.update(task_id, "blocked", error="执行进程异常退出，保留现场供审查")

    def launch(task_id, key=""):
        """启动独立进程并跟踪生命周期；密钥不出现在命令行和任务文件中。"""
        env = os.environ.copy()
        if key:
            env["AICD_WEB_API_KEY"] = key
        try:
            process = subprocess.Popen([sys.executable, "-m", "web_runtime.worker", str(root / task_id)],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                start_new_session=os.name != "nt")
        except OSError:
            store.update(task_id, "blocked", error="任务进程启动失败")
            return
        processes[task_id] = process
        asyncio.create_task(monitor(task_id, process))

    async def stop(task_id):
        """停止进程树并清理任务标签容器；失败时报告 blocked 并保留文件。"""
        from graph.test_runner import PytestRunner
        process = processes.get(task_id)
        if process and process.poll() is None:
            await asyncio.to_thread(PytestRunner._stop, process)
        path = root / task_id / "task.json"
        if path.exists():
            from mcp_servers.context import TaskConfig
            from graph.editor import Workspace
            from sandbox.docker_runner import DockerRunner
            task = TaskConfig.model_validate_json(path.read_text(encoding="utf-8"))
            runner = DockerRunner(Workspace(task.workspace, trusted_copy=True), task.tests, task.image,
                timeout=task.timeout, task_id=task_id, artifacts=path.parent)
            await asyncio.to_thread(runner.cleanup_remaining)

    @asynccontextmanager
    async def lifespan(app):
        """启动时阻塞不确定任务；关闭时回收进程，保留审批等待点。"""
        for item in store.listing():
            if item["status"] in {"running", "queued"}:
                store.update(item["id"], "blocked", error="服务中断，禁止自动重放未确认操作")
                await stop(item["id"])
        yield
        for task_id in list(processes):
            await stop(task_id)

    app = FastAPI(lifespan=lifespan)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        """校验 Host 和写请求令牌，防止恶意网页访问本地执行 API。"""
        if request.headers.get("host", "").split(":")[0] not in {"127.0.0.1", "localhost", "testserver"}:
            return HTMLResponse("拒绝非本地主机", status_code=403)
        if request.method != "GET" and request.headers.get("x-local-token") != token:
            return HTMLResponse("本地令牌无效", status_code=403)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    def home():
        """返回无外部脚本的本地交互页面及本次服务令牌。"""
        return Path(__file__).with_name("index.html").read_text(encoding="utf-8").replace("__TOKEN__", token)

    @app.get("/api/tasks")
    def history():
        """列出持久任务历史。"""
        return store.listing()

    @app.get("/api/defaults")
    def defaults():
        """返回非敏感默认参数；凭据只返回是否已配置，不返回密钥值。"""
        import config
        return {"model": config.LLM_MODEL, "base_url": config.LLM_BASE_URL,
                "max_tokens": min(16384, max(256, config.MAX_TOKENS)),
                "context_chars": 24000, "max_iterations": 3, "timeout": 60,
                "api_key_configured": bool(os.environ.get("AICD_WEB_API_KEY") or os.environ.get("DASHSCOPE_API_KEY"))}

    @app.post("/api/repositories/select")
    def select_repository():
        """打开本机文件夹选择器，并返回该仓库可选文件；取消时保留页面原选择。"""
        from web_runtime.options import pick_repository, repository_options
        try:
            selected = pick_repository()
            return repository_options(selected) if selected else {"cancelled": True}
        except (OSError, ValueError, RuntimeError):
            raise HTTPException(422, "无法选择仓库，请确认图形环境可用并选择 Git 根目录")

    @app.post("/api/options/images")
    def images():
        """提供当前本地镜像候选项；不允许页面传入 Docker 命令参数。"""
        from web_runtime.options import local_images
        return local_images()

    @app.get("/api/tasks/{task_id}")
    def detail(task_id: str):
        """读取任务结果；未知 ID 返回 404。"""
        try:
            return store.get(task_id)
        except KeyError:
            raise HTTPException(404, "任务不存在")

    @app.post("/api/tasks", status_code=202)
    async def create(body: TaskInput):
        """接受任务后立即返回 ID；仓库及工具安全检查在独立工作进程执行。"""
        if not body.trusted_copy or not body.request.strip():
            raise HTTPException(422, "必须确认可信仓库并填写需求")
        task_id = uuid4().hex
        (root / task_id).mkdir()
        store.create(task_id, {"options": body.model_dump(exclude={"api_key"})})
        store.claim(task_id, "queued")
        launch(task_id, body.api_key)
        return {"task_id": task_id}

    @app.get("/api/tasks/{task_id}/diff")
    def current_diff(task_id: str):
        """读取工作树当前 Git Diff，避免页面刷新只看到最后一次保存的快照。"""
        detail(task_id)
        path = root / task_id / "task.json"
        if not path.exists():
            return {"diff": ""}
        from mcp_servers.context import TaskConfig
        from workspace.manager import git
        task = TaskConfig.model_validate_json(path.read_text(encoding="utf-8"))
        value = git(Path(task.workspace), "diff", "--no-ext-diff", "--no-textconv",
                    task.base_commit, "--", *task.allowed_files)
        if len(value.encode("utf-8")) > 524288:
            raise HTTPException(409, "Diff 超过显示上限")
        return {"diff": value}

    @app.post("/api/tasks/{task_id}/approve")
    async def approve(task_id: str, body: Decision):
        """核对等待点并原子领取；重复审批不会再次启动工作进程。"""
        item = detail(task_id)
        if item.get("approval", {}).get("id") != body.approval_id or not store.claim(task_id, "waiting"):
            raise HTTPException(409, "审批已处理或等待点已改变")
        store.update(task_id, "running", decision=body.model_dump(exclude={"api_key", "approval_id"}))
        launch(task_id, body.api_key)
        return {"status": "running"}

    @app.get("/api/tasks/{task_id}/apply-preview")
    def apply_preview(task_id: str):
        """返回已核验的源仓库与文件清单，供用户应用前确认。"""
        from web_runtime.apply_changes import prepare_application
        item = detail(task_id)
        if item.get("application_status") == "applied":
            return {"applied": True, "files": item.get("applied_changes", [])}
        try:
            with application_lock:
                return prepare_application(root / task_id, item)[2]
        except (OSError, ValueError, RuntimeError):
            raise HTTPException(409, "无法应用：请确认任务验收成功，原仓库干净且提交未改变，修复文件未被修改")

    @app.post("/api/tasks/{task_id}/apply")
    def apply_to_repository(task_id: str, body: ApplyDecision):
        """明确确认后应用修改；串行化应用请求，持久标记防止重复写入。"""
        from web_runtime.apply_changes import prepare_application, apply_application
        if not body.confirm:
            raise HTTPException(422, "需要明确确认应用修改")
        with application_lock:
            item = detail(task_id)
            if item.get("application_status") == "applied":
                return {"applied": True, "changes": item["applied_changes"]}
            if item.get("application_status") in {"applying", "uncertain"}:
                raise HTTPException(409, "上次应用结果未确认，请人工检查原仓库")
            try:
                editor, batch, preview = prepare_application(root / task_id, item)
            except (OSError, ValueError, RuntimeError):
                raise HTTPException(409, "原仓库或修复文件已变化，或任务未通过验收，拒绝应用")
            # 在任何写入前持久记录意图；进程中途退出后不自动重放。
            store.update(task_id, item["status"], application_status="applying")
            try:
                records = apply_application(editor, batch)
            except (OSError, ValueError, RuntimeError, SyntaxError):
                store.update(task_id, item["status"], application_status="uncertain")
                raise HTTPException(409, "应用失败，请检查原仓库；不会自动覆盖或重试")
            store.update(task_id, item["status"], application_status="applied", applied_changes=records,
                         applied_repository=preview["repository"])
            return {"applied": True, "changes": records}

    @app.post("/api/tasks/{task_id}/{action}")
    async def control(task_id: str, action: str):
        """暂停在审批等待点，或取消任务并回收进程；拒绝不安全的节点中途恢复。"""
        item = detail(task_id)
        if action == "pause" and item["status"] == "running":
            store.update(task_id, "running", pause_requested=True)
        elif action == "pause" and store.claim(task_id, "waiting"):
            store.update(task_id, "paused")
        elif action == "resume" and store.claim(task_id, "paused"):
            store.update(task_id, "waiting")
        elif action == "cancel" and item["status"] in {"running", "waiting", "paused"}:
            store.update(task_id, "cancelling")
            try:
                await stop(task_id)
                store.update(task_id, "cancelled")
            except Exception:
                store.update(task_id, "blocked", error="取消时清理失败，请检查 Docker")
        else:
            raise HTTPException(409, "当前状态不支持该操作；暂停需等待审批点")
        return detail(task_id)

    @app.get("/api/tasks/{task_id}/events")
    async def events(task_id: str, request: Request, after: int = 0):
        """通过持久序号重放 SSE；断开连接后停止轮询，不中断后台任务。"""
        detail(task_id)
        after = max(after, int(request.headers.get("last-event-id", "0")))

        async def stream():
            """轮询 SQLite 并发送心跳；没有阻塞数据库长事务。"""
            cursor = after
            while not await request.is_disconnected():
                for seq, data in store.events(task_id, cursor):
                    cursor = seq
                    yield f"id: {seq}\ndata: {data}\n\n"
                yield ": heartbeat\n\n"
                await asyncio.sleep(1)
        return StreamingResponse(stream(), media_type="text/event-stream")

    return app


app = create_app()
#python -m uvicorn web_runtime.app:app --host 127.0.0.1 --port 8000 --workers 1
