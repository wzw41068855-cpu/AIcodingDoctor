

**面向 Python Git 仓库的智能代码问答与缺陷修复助手。**

基于 LangGraph 构建 Supervisor + 专业 Agent 协作机制，让模型自主检索源码、选择工具、分析失败并迭代修复；通过 MCP 权限校验、Git Worktree、Docker 测试和人工审批约束执行过程。

## 技术栈

| 层次 | 技术 | 在项目中的作用 |
| --- | --- | --- |
| 语言与服务 | Python、FastAPI、Uvicorn | 本地 Web 服务、任务 API 与 Worker |
| Agent 编排 | LangGraph、LangChain Core、Pydantic | 阶段调度、专业 Agent 工具循环、结构化动作校验 |
| 模型服务 | OpenAI 兼容接口、DashScope | 大模型推理、Embedding 与 Rerank |
| 代码检索 | Python AST、Chroma、BM25、RRF | 语义与关键词混合召回、父子源码扩展 |
| 工具协议 | 官方 MCP SDK、stdio | 隔离工具进程、角色身份与工具权限检查 |
| 修改与测试 | Git Worktree、Docker、pytest | 隔离工作树、受控测试执行、JUnit 结果解析 |
| 状态与记忆 | SQLite、LangGraph SQLite Checkpoint | 任务与审批持久化、上下文记忆、操作对账 |
| 前端与观测 | 原生 HTML / CSS / JavaScript、SSE | 双模式工作台、执行事件、Diff 与预算展示 |
| 验证 | pytest、Node 内置测试器 | 后端回归、前端交互与显式开启的真实联调 |

## 项目亮点

### 1. 阶段级多 Agent，自主选择工具

Supervisor 根据任务状态决定下一阶段；Explorer、Coder、Tester、Reviewer 在各自子图内连续调用工具，遇到阶段完成、重要决策或预算耗尽时返回 Supervisor，减少逐工具重新调度的模型调用。

| Agent | 职责 | 权限边界 |
| --- | --- | --- |
| Supervisor | 理解目标、阶段调度、预算与终止决策 | 读取任务状态与调度 |
| Explorer | 检索文件、定位符号、分析错误与静态引用 | 搜索、读取、Git 只读 |
| Coder | 生成补丁、根据失败反馈调整修改 | 读取、有限搜索、受审批约束的编辑 |
| Tester | 选择授权测试、分析失败 | 受限测试执行；新增回归测试需额外授权 |
| Reviewer | 独立检查 Diff、结果与潜在回归 | 只读源码、Diff 和测试结果 |

### 2. 从代码片段召回到当前源码证据

AST 优先切分代码，结合向量检索与 BM25，通过 RRF 融合、Rerank 重排。小块用于召回，父级源码用于理解；同时提供版本和 stale 标记，并以实际读取的当前文件复核回答引用，输出文件路径和行号。静态引用搜索可辅助追踪调用位置。

### 3. 分层记忆与上下文预算

Context Builder 按角色装配近期对话、结构化事实、滚动摘要和工具结果。大型结果卸载为可分页读取的 Artifact，避免每轮重复传入全文；项目约定和排障经验由用户显式保存，按仓库隔离，支持查看、修改与删除。

### 4. 自主修复具有明确的执行边界

修复在独立 Git Worktree 中进行，候选补丁先展示真实 Diff，审批绑定补丁与源文件哈希。测试仅执行授权目标，在非特权、断网、资源受限的 Docker 容器内运行。最终写回原仓库需再次确认，不自动提交 Git。角色工具白名单在 MCP 执行端校验。

### 5. 执行过程可观察、可控制、可核查

Web 展示阶段、工具行动说明、预算、Diff、测试结果和剩余问题，支持反馈、审批、暂停与取消。SQLite 保存事件、审批和操作记录；恢复先核对副作用，结果不确定时进入 blocked，保留现场供人工核查。


## 下载与启动

### 1. 准备环境

- 建议使用 **Python 3.13**，并安装 Git。
- 修复功能额外需要 Docker，使用 Linux 容器模式；仅体验问答可以不安装 Docker。
- 准备模型服务凭据：LLM、Embedding、Rerank 均需可访问。默认配置使用 DashScope，具体模型及端点见 [config.py](config.py)。
- 待分析项目应为可信的本地 Python Git 仓库，已有至少一次提交；修复前提交或备份当前改动。

当前使用 `requirements.txt` 管理依赖，没有 `pyproject.toml`。前端无需 npm 安装或构建。

### 2. 克隆仓库并安装依赖

```bash
git clone https://github.com/wzw41068855-cpu/AIcodingDoctor.git
cd AIcodingDoctor
python -m venv .venv
```

Windows PowerShell 激活环境：

```powershell
.\.venv\Scripts\Activate.ps1
```

macOS / Linux 激活环境：

```bash
source .venv/bin/activate
```

安装依赖：

```bash
python -m pip install -r requirements.txt
```


### 3. 配置模型凭据

将 `.env.example` 复制为项目根目录下的 `.env`，替换示例值：

```dotenv
DASHSCOPE_API_KEY=你的真实凭据
```


### 4. 启动 Web 工作台

```bash
python main.py
```

打开 **http://127.0.0.1:8000/**。更换端口可使用 `python main.py --port 8001`。

也可显式使用 Uvicorn：

```bash
python -m uvicorn web_runtime.app:app --host 127.0.0.1 --port 8000 --workers 1
```

保持本机回环地址和单 Worker；当前调度器在进程内管理任务进程句柄。

## 使用教程

### 代码问答：先体验一个无需 Docker 的任务

1. 打开“代码问答”，选择本地 Git 仓库根目录，确认只读分析授权。
2. 确认模型与凭据，输入问题，例如：`这个项目的请求从 API 到 Service 是如何流转的？请给出文件和行号。`
3. 在执行详情查看搜索、读取与阶段进度，等待附带源码证据的回答。
4. 在输入框继续追问，例如：`如果参数为空，会在哪一层报错？`；也可打开历史或点击“新对话”。

Enter 发送，Shift + Enter 换行。回答完成后整条展示，SSE 更新执行状态；当前没有逐 Token 输出回答。

### 代码修复：准备测试镜像

测试容器断网，镜像必须预先安装 **pytest 和待修复项目的测试依赖**。系统不会自动拉取或构建镜像，普通 `python:3.13` 镜像通常不满足测试要求。

例如，在独立目录创建 `Dockerfile`，为仅依赖标准库的待测项目准备最小镜像：

```dockerfile
FROM python:3.13-slim
RUN python -m pip install --no-cache-dir pytest
ENTRYPOINT ["python"]
```

在该目录构建并检查：

```bash
docker build -t aicd-pytest:local .
docker run --rm aicd-pytest:local -m pytest --version
```

实际项目若依赖 FastAPI 等包，应在构建阶段一并安装对应依赖；这里的镜像只适合上述最小示例。镜像需要提供 `python` 入口，运行期不能临时联网安装依赖。

### 提交修复并检查结果

1. 切换“代码修复”，选择可信仓库，填写错误现象、预期行为或失败日志。
2. 选择允许修改的业务文件；留空时允许 Agent 在系统支持的业务文件范围内自主定位，不代表任意文件写权限。
3. 选择已有测试文件或目录，以及本地镜像，例如 `aicd-pytest:local`。
4. 提交任务，检查阶段计划和候选补丁 Diff；批准后才应用到隔离工作树。拒绝时可填写调整意见。
5. 审批测试执行，观察真实测试结果；Agent 可根据失败继续探索或修复，Reviewer 独立检查变更。
6. 检查最终报告、Diff 和测试结果，需要保留修改时再确认“应用到原仓库”，随后自行检查并提交 Git。

默认禁止新增回归测试；API 的 `allow_regression_tests` 可显式授权受控新建，不能覆盖既有测试。运行中补充反馈不会扩大原有文件和测试授权。切换问答/修复入口不会取消后台任务。

### 常见问题

| 现象 | 检查方法 |
| --- | --- |
| 无法选择项目或没有文件候选 | 选择 Git 根目录，确认文件已提交；配置文件、测试文件和链接等可能被修复策略排除 |
| 模型能回答，但检索认证失败 | 分别检查 Embedding、Rerank 的凭据与服务权限 |
| 镜像列表为空或 Docker 不可用 | 启动 Docker，确认 Linux 容器模式，手动构建镜像后刷新 |
| 测试提示缺少模块 | 将待测项目依赖预装进镜像，容器运行时不能联网安装 |
| 任务进入 blocked | 查看报告和操作记录；授权不足、环境异常或未知副作用需人工核查 |
| 服务重启后页面 Key 失效 | 重新填写，或在服务启动环境中配置凭据 |

