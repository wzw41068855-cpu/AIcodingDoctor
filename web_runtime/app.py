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
from fastapi.responses import HTMLResponse, StreamingResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from graph.budget import Limits
from web_runtime.store import Store


class TaskInput(BaseModel):
    """用户固定授权参数；密钥仅转交子进程环境，不写 SQLite。"""
    model_config = ConfigDict(extra="forbid")
    repository: str
    request: str = Field(min_length=1, max_length=4000)
    mode: str = Field(default="repair", pattern="^(repair|ask)$")
    trusted_copy: bool
    allowed_files: list[str] = Field(default_factory=list, max_length=8)
    tests: list[str] = Field(default_factory=lambda: ["tests"], max_length=8)
    image: str = "python:3.13"
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    embedding_api_key: str = ""
    rerank_api_key: str = ""
    max_tokens: int = Field(default=4096, ge=256, le=16384)
    context_chars: int = Field(default=24000, ge=4000, le=24000)
    max_iterations: int = Field(default=3, ge=1, le=5)
    timeout: float = Field(default=60, gt=0, le=300)
    budget: Limits = Field(default_factory=Limits)
    allow_regression_tests: bool = False
    conversation: list[dict] = Field(default_factory=list, max_length=100)


class Decision(BaseModel):
    """审批绑定中断 ID，避免旧页面批准新一轮操作。"""
    approval_id: str
    approve: bool
    feedback: str = Field(default="", max_length=2000)
    api_key: str = ""
    embedding_api_key: str = ""
    rerank_api_key: str = ""


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
    task_keys: dict[str, dict[str, str]] = {}

    #创建一个线程锁，用于在多线程环境中保护对共享资源的访问，确保任务调度和状态更新的线程安全。
    application_lock = Lock()

    async def monitor(task_id, process):
        """后台等待进程退出；未到持久等待点的异常退出禁止自动重放。"""
        await asyncio.to_thread(process.wait)
        # 老审批进程可能晚于新 Worker 退出，不能清除或阻塞新进程的任务。
        if processes.get(task_id) is not process:
            return
        processes.pop(task_id, None)
        if store.get(task_id)["status"] == "running":
            store.update(task_id, "blocked", error="执行进程异常退出，保留现场供审查")

    def launch(task_id, key="", embedding_key="", rerank_key=""):
        """启动独立进程,执行任务，并跟踪生命周期；密钥不出现在命令行和任务文件中。"""
        keys = task_keys.setdefault(task_id, {})
        for name, value in {"AICD_WEB_API_KEY": key, "AICD_EMBEDDING_API_KEY": embedding_key,
                            "AICD_RERANK_API_KEY": rerank_key}.items():
            if value:
                keys[name] = value
        env = os.environ.copy()
        env.update(keys)
        if key:
            env["AICD_WEB_API_KEY"] = key
        if embedding_key:
            env["AICD_EMBEDDING_API_KEY"] = embedding_key
        if rerank_key:
            env["AICD_RERANK_API_KEY"] = rerank_key
        try:
            #subprocess.Popen(...) 启动一个新的 Python 进程来运行 web_runtime.worker 模块，并传入任务目录作为参数。
            process = subprocess.Popen([sys.executable, "-m", "web_runtime.worker", str(root / task_id)],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                start_new_session=os.name != "nt")
        except OSError:
            store.update(task_id, "blocked", error="任务进程启动失败")
            return
        processes[task_id] = process#存储该任务的进程对象，以便后续可以监控和管理它。
        asyncio.create_task(monitor(task_id, process))#创建一个异步任务来监控该进程的状态，确保在进程退出时更新任务状态。

    async def stop(task_id):
        """停止进程树并清理任务标签容器；失败时报告 blocked 并保留文件。
        目标，是安全停止某个任务对应的执行现场：
        先处理 Python 子进程，再清理它可能留下的容器/资源。
        它是异步函数，但里面很多动作本身是阻塞的，所以代码用 asyncio.to_thread(...) 把这些阻塞操作丢到线程池里执行，
        避免卡住 FastAPI 的事件循环。
        """
        from graph.test_runner import PytestRunner
        process = processes.get(task_id)
        if process and process.poll() is None:#如果进程仍在运行test，先尝试终止它。
            #PytestRunner._stop是阻塞的，可能会卡住事件循环，所以用 asyncio.to_thread(...) 把它丢到线程池里执行。
            await asyncio.to_thread(PytestRunner._stop, process)
        path = root / task_id / "task.json"
        if path.exists():
            from mcp_servers.context import TaskConfig
            from graph.editor import Workspace
            from sandbox.docker_runner import DockerRunner
            #。TaskConfig.model_validate_json(...) 同时完成“读取 + 结构校验”，保证后面构造运行器时字段类型是可信的。
            task = TaskConfig.model_validate_json(path.read_text(encoding="utf-8"))
            if task.readonly:
                return
            #代码临时构造一个 DockerRunner，然后调用清理：
            runner = DockerRunner(Workspace(task.workspace, trusted_copy=True), task.tests, task.image,
                timeout=task.timeout, task_id=task_id, artifacts=path.parent)
            # 这一步借助 DockerRunner 的清理方法，把任务可能遗留的容器或标签资源回收掉。
            await asyncio.to_thread(runner.cleanup_remaining)

    @asynccontextmanager
    async def lifespan(app):
        """启动时阻塞不确定任务；关闭时回收进程，保留审批等待点。"""
        for item in store.listing():
            if item["status"] in {"running", "queued", "cancelling"}:
                #这体现了一个明确的安全策略：服务重启后，不自动继续之前“未确认完成”的任务。
                store.update(item["id"], "blocked", error="服务中断，禁止自动重放未确认操作")
                await stop(item["id"])
        yield
        for task_id in list(processes):
            #关闭服务时，遍历所有正在运行的任务进程，调用 stop() 来安全地终止它们，并清理相关资源。
            await stop(task_id)

    #lifespan意思是：应用“开始提供服务之前”会先执行 yield 前的逻辑；应用“准备关闭时”会执行 yield 后的逻辑。
    #它相当于把全局的启动和收尾策略集中在一个地方，而不是分散在多个事件处理函数里。
    app = FastAPI(lifespan=lifespan)

    #一个全局 HTTP 中间件，每个请求进入具体路由之前，都会先经过 local_only(...)。
    # 核心流程是：先做安全检查，检查通过才继续执行后面的接口处理。
    @app.middleware("http")
    async def local_only(request: Request, call_next):
        """校验 Host 和写请求令牌，防止恶意网页访问本地执行 API。"""
        #第一层校验限制请求主机必须是本机，防止外部网络访问本地服务。
        if request.headers.get("host", "").split(":")[0] not in {"127.0.0.1", "localhost", "testserver"}:
            return HTMLResponse("拒绝非本地主机", status_code=403)
        #第二层校验针对非 GET 请求（通常是会改状态的写操作）要求携带本地令牌：
        if request.method != "GET" and request.headers.get("x-local-token") != token:
            return HTMLResponse("本地令牌无效", status_code=403)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    def home():
        """返回无外部脚本的本地交互页面及本次服务令牌。"""
        #服务启动时生成的 token 会被注入到页面里，前端 JS 可以在发起写请求时带上这个 token，后端中间件会验证它。
        return Path(__file__).with_name("index.html").read_text(encoding="utf-8").replace("__TOKEN__", token)

    @app.get("/workspace.js")
    def workspace_script() -> Response:
        """返回本地功能脚本，不加载第三方资源。"""
        return Response(Path(__file__).with_name("workspace.js").read_text(encoding="utf-8"), media_type="text/javascript")

    @app.get("/api/tasks")
    def history():
        """列出持久任务历史。"""
        return store.listing()#返回所有任务的状态列表，供前端展示历史记录。

    @app.get("/api/defaults")
    def defaults():
        """返回非敏感默认参数；凭据只返回是否已配置，不返回密钥值。"""
        import config
        #返回默认的模型配置和一些限制参数，确保前端知道当前服务的能力范围和限制条件。
        return {"model": config.LLM_MODEL, "base_url": config.LLM_BASE_URL,
                "max_tokens": min(16384, max(256, config.MAX_TOKENS)),
                "context_chars": 24000, "max_iterations": 3, "timeout": 60,
                "api_key_configured": bool(os.environ.get("AICD_WEB_API_KEY") or os.environ.get("DASHSCOPE_API_KEY"))}

    @app.post("/api/repositories/select")
    def select_repository():
        """打开本机文件夹选择器，并返回该仓库可选文件；取消时保留页面原选择。"""
        from web_runtime.options import pick_repository, repository_options
        try:
            selected = pick_repository()#pick_repository() 负责弹出本地文件夹选择器
            return repository_options(selected) if selected else {"cancelled": True}
        except (OSError, ValueError, RuntimeError):
            raise HTTPException(422, "无法选择仓库，请确认图形环境可用并选择 Git 根目录")

    @app.post("/api/options/images")
    def images():
        """提供当前本地镜像候选项；不允许页面传入 Docker 命令参数。"""
        from web_runtime.options import local_images
        return local_images()#返回当前系统中可用的 Docker 镜像列表，供前端选择使用。

    @app.get("/api/tasks/{task_id}")
    def detail(task_id: str):
        """读取任务结果；未知 ID 返回 404。"""
        try:
            return store.get(task_id)
            #返回指定任务的详细信息，包括状态、进度、结果等。如果任务不存在，则抛出 HTTP 404 异常。
        except KeyError:
            raise HTTPException(404, "任务不存在")

    @app.post("/api/tasks", status_code=202)
    async def create(body: TaskInput):
        """接受任务后立即返回 ID；仓库及工具安全检查在独立工作进程执行。"""
        if not body.trusted_copy or not body.request.strip():
            raise HTTPException(422, "必须确认可信仓库并填写需求")
        task_id = uuid4().hex#唯一的任务标识
        (root / task_id).mkdir()
        #目录按任务隔离，后续产物（如 task.json、日志、工件）都可以放在这个独立路径里
        store.create(task_id, {"options": body.model_dump(exclude={"api_key", "embedding_api_key", "rerank_api_key"})})
        store.claim(task_id, "queued")#标记任务为已排队状态，claim乐观锁，防止重复处理。
        launch(task_id, body.api_key, body.embedding_api_key, body.rerank_api_key)
        return {"task_id": task_id}

    @app.post("/api/tasks/{task_id}/followup", status_code=202)
    async def followup(task_id: str, body: Decision):
        """在已结束的只读会话上创建连续追问，不恢复副作用任务。"""
        item = detail(task_id)
        if item["options"]["mode"] != "ask" or item["status"] != "success" or not body.feedback.strip():
            raise HTTPException(409, "仅已完成的问答可继续追问")
        options = dict(item["options"])
        options["conversation"] = (options.get("conversation", []) + [
            {"role": "user", "summary": options["request"]},
            {"role": "assistant", "summary": item.get("answer", "")}])[-100:]
        options["request"] = body.feedback
        return await create(TaskInput(**options, api_key=body.api_key, embedding_api_key=body.embedding_api_key, rerank_api_key=body.rerank_api_key))

    @app.get("/api/tasks/{task_id}/diff")
    def current_diff(task_id: str):
        """读取工作树当前 Git Diff，避免页面刷新只看到最后一次保存的快照。"""
        detail(task_id)#复用已有的任务存在性检查逻辑。若任务不存在，detail 会抛 404，
        path = root / task_id / "task.json"
        if not path.exists():
            return {"diff": ""}
        from mcp_servers.context import TaskConfig
        from workspace.manager import git
        task = TaskConfig.model_validate_json(path.read_text(encoding="utf-8"))
        if task.readonly:
            return {"diff": ""}
        value = git(Path(task.workspace), "diff", "--no-ext-diff", "--no-textconv",
                    task.base_commit, "--", *task.allowed_files)
        if len(value.encode("utf-8")) > 524288:
            raise HTTPException(409, "Diff 超过显示上限")
        return {"diff": value}

    @app.post("/api/tasks/{task_id}/approve")
    async def approve(task_id: str, body: Decision):
        """核对等待点并原子领取；重复审批不会再次启动工作进程。"""
        item = detail(task_id)
        if not store.claim(task_id, "waiting", body.approval_id):
            raise HTTPException(409, "审批已处理或等待点已改变")
        store.update(task_id, "running", decision=body.model_dump(exclude={"api_key", "embedding_api_key", "rerank_api_key", "approval_id"}))
        launch(task_id, body.api_key, body.embedding_api_key, body.rerank_api_key)
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
                from graph.operations import OperationJournal
                (root / task_id).mkdir(exist_ok=True)
                records = OperationJournal(root / task_id / "operations.sqlite").run(
                    "repository.apply", {"batch": batch.model_dump() if batch else None, "preview": preview},
                    lambda: apply_application(editor, batch))
            except (OSError, ValueError, RuntimeError, SyntaxError):
                store.update(task_id, item["status"], application_status="uncertain")
                raise HTTPException(409, "应用失败，请检查原仓库；不会自动覆盖或重试")
            store.update(task_id, item["status"], application_status="applied", applied_changes=records,
                         applied_repository=preview["repository"])
            return {"applied": True, "changes": records}

    from web_runtime.memory_routes import register_memory_routes
    register_memory_routes(app, root, store)

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
