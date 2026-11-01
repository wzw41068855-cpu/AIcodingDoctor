> **注意：此文档记录原 V4 功能。精简后的项目运行入口与已知问题参见 [`README.md`](../README.md) 和 [`REFACTOR_V4.md`](REFACTOR_V4.md)。**

# V4 本地 Web 工作台

## 启动

在项目配置的 Python 环境安装 `requirements.txt` 后启动：

```powershell
python -m uvicorn web_runtime.app:app --host 127.0.0.1 --port 8000 --workers 1
```

打开 `http://127.0.0.1:8000`。不要启用多 worker，不要绑定公网。
点击“选择文件夹”打开服务所在电脑的系统目录对话框，选择 Git 仓库根目录。
选择后，业务 Python 文件和测试路径自动填入多选菜单；Docker 镜像由本地
镜像列表提供，可点击刷新。没有候选项时应先准备仓库测试或本地测试镜像。
此交互不上传源码，依赖本机 Python 的 tkinter 和可用的桌面会话；
取消对话框保留原选择，图形环境不可用时页面提示选择失败。
源仓库必须干净；未提交修改会被拒绝，不会自动提交或清理。
修复保留在独立 worktree，用户审查后自行决定如何合并。
成功任务的 Diff 下方提供“应用修改到原仓库”按钮，先预览目标仓库及文件清单，
确认后写回原仓库工作区，不自动暂存或提交 Git。应用前要求原仓库干净、HEAD
仍为任务基线，并核验源文件及修复文件哈希；冲突、未验收或篡改会拒绝应用。
重复请求不会重复写入。中途退出或回滚失败的应用需人工检查，不自动重试。

模型可以设置名称、OpenAI 兼容 API 地址、API Key、输出 Token 上限、
上下文字符上限、修复轮次和测试超时。上下文字符限制不是模型 Token 窗口，
超限时阻塞，不截断待修改源码。Embedding/Rerank 沿用原 V1 配置和凭据。
密钥不写任务数据库或命令行；审批恢复时如宿主没有相应环境凭据，
需重新填写页面密钥。页面刷新不会保存密钥。
模型、API 地址和输出 Token 默认值取自 `config.py`；其余参数提供现有默认值。
API Key 使用密码框；环境已配置凭据时仅显示掩码占位提示，接口不返回密钥。

只读问答通过 V1 检索服务经 MCP 获取证据后回答，不进入修复图，
不运行 Docker；目前仍使用干净 Git worktree 和明确的源码路径参数。
修复任务使用已安装 pytest 和目标依赖的本地 Docker 镜像，不自动拉取镜像。

## 持久化和流程

- `web_runtime/app.py`：薄路由、子进程调度、本机访问校验、SSE 重放。
- `web_runtime/store.py`：SQLite 任务、事件、状态条件领取。
- `web_runtime/worker.py`：独立任务进程，复用 V3 worktree、MCP、Docker。
- `web_runtime/workflow.py`：复用 Planner/Coder/Tester/Reviewer/Finalizer，
  用独立审批节点调用官方 interrupt / Command，不让旧异常守卫吞掉中断。
- `web_runtime/index.html`：原生 HTML/JS，无前端依赖，不直接访问 MCP。
- `mcp_servers/context.py`：会话重建时从固定提交恢复白名单修改基线。
- `graph/nodes.py`：保留默认 24000 字符限制，允许任务设置更小上下文上限。
- `requirements.txt`：新增 FastAPI、Uvicorn、官方 SQLite Checkpointer。

数据默认保存在系统临时目录下固定的 `aicd-v4`，每个任务独立子目录，
共享 `tasks.sqlite` 保存任务与事件，各任务 `graph.sqlite` 保存 Graph 状态。
此位置会受到系统临时文件清理影响。长期使用可在自定义启动入口调用
`create_app(storage=绝对路径)`，该目录必须在源码仓库及目标仓库之外。
数据库包含代码上下文与任务历史，应按本机用户资料保护，不上传或提交。

任务日志位于 `%TEMP%\aicd-v4\<task_id>\`：`audit.jsonl` 记录工具操作，
`diagnostics.jsonl` 记录失败阶段、异常类型和栈位置，Docker 测试日志为
`aicd-*.log`。`tasks.sqlite` 保存任务状态及历史事件。诊断不记录 SDK
异常原文、源码行或局部变量；已知的源仓库非干净错误会显示明确解决提示。
网页失败详情同时显示诊断路径。旧任务不会自动补写新格式诊断。
异常组诊断同时保留内部异常类型和栈位置；仍不记录异常原文。
节点通过 Graph 的 updates 流输出，网页分卡片显示 Planner、Coder、Tester、
Reviewer 和 Finalizer 的最近结果，历史轮次可通过持久事件追溯。
运行时显示转圈动画，等待审批与暂停显示 ⏸️，错误显示 ❌，完成显示 ✅。
SQLite 存储递归转换嵌套 Pydantic 对象，支持 Coder 的修改记录列表。

Docker 的 JUnit 报告写入独立宿主临时目录，挂载到容器 `/reports`，
容器退出后直接读取、校验并清理；源码 `/input` 仍只读，工作树、索引和
任务产物目录不会以可写方式挂载。报告大小上限 4 MB，拒绝链接及特殊文件。
不再从容器退出后已消失的 `/tmp` tmpfs 中执行 `docker cp`。
报告目录的写入总量没有独立磁盘配额，测试仓库仍必须可信。

计划审批通过后还需本轮文件修改审批；每轮测试单独审批固定 Docker pytest。
拒绝计划并填写意见会重新调用 Planner；拒绝且无意见终止修改。
拒绝修改或测试进入 Finalizer。审批携带中断 ID，状态条件更新防止重复启动。
修改审批针对本轮文件范围，并非逐行补丁预览；Diff 在修改后提供。

运行中暂停是请求在下一个持久审批点暂停，不抢占正在写文件的节点。
取消会结束任务进程树，并按任务标签回收容器，保留源码和审计。
普通服务关闭会清理已跟踪进程；中断等待点可在服务重启后继续审批。
未确认完成的运行任务在重启后标记 blocked，不自动重放写操作。
强制杀死 Web 服务可能遗留宿主工作进程；目前没有操作系统 Job Object
或独立监督进程，需人工核对进程与容器。因此异常崩溃清理尚未达到完整验收。

## 测试与人工验收

```powershell
python -m pytest tests/test_v4_runtime.py tests/test_v2_workflow.py tests/v3 -q -p no:cacheprovider
```

离线测试使用临时 Git 仓库、模拟模型/索引/容器结果及真实 MCP 协议；
V4 闭环覆盖每次审批后关闭数据库和 MCP 会话、重新构建图恢复。
这些测试不证明真实 Docker、外部 API 或浏览器操作已通过。

人工验收顺序：提交真实修复任务 → 拒绝计划并提出意见 → 批准新计划 →
批准文件修改 → 查看 Diff → 批准 pytest → 查看错误及重试轮次 →
刷新页面 → 在审批点重启服务并恢复 → 查看最终报告。
另需验证同时运行两个任务、重复点击审批、测试期间取消及容器无残留。

当前尚需真实模型、Embedding/Rerank、Docker 镜像和浏览器端联合验收；
未完成该验收及异常崩溃进程回收之前，不宣称 V4 全部完成。

本次执行记录：

- `python -m pytest tests/test_v4_runtime.py tests/test_v2_workflow.py tests/v3 -q -p no:cacheprovider`：
  66 通过、0 失败、2 跳过。跳过原因是未设置 `AICD_V3_TEST_IMAGE` 和
  `AICD_V3_SECURITY_IMAGE`，未执行真实 Docker 测试。
- 最后补充进程启动失败及重复审批 API 测试后，
  `python -m pytest tests/test_v4_runtime.py -q -p no:cacheprovider`：5 通过、0 失败。
- 初次受限测试：18 通过、2 跳过、45 环境错误（系统临时目录权限不足），
  授权后重跑得到上述通过结果。
- SQLite 官方包经 pip 安装为 3.1.3，Checkpoint 从 4.3.0 更新至 4.3.1；
  Conda 源没有该包。安装器另外报告环境已有 `langgraph-api 0.10.3`
  与 `sse-starlette 3.5.0` 版本不兼容，未修改这些无关依赖。
- 未执行全仓测试、外部模型/API 联调或真实浏览器端到端验收。

官方 API 依据：
[interrupt](https://reference.langchain.com/python/langgraph/types/interrupt)、
[Command](https://reference.langchain.com/python/langgraph/types/Command)。
