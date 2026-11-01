# AICodingDoctor V4 — 精简版

本项目是 **本地 Web AI Coding Agent**，针对可信的 Python Git 仓库，提供代码问答及带人工审批的代码修复。

> 本包基于 V1→V4 逐步迭代的原项目做**结构与冗余清理**，不等于新的功能版本。修复路径的已知问题尚未处理，详见 [重构记录](docs/REFACTOR_V4.md)。

## V4 保留的功能

- 读取本地 Git 仓库；AST-first 切分 Python、Markdown 文件，Chroma + BM25 + RRF + Rerank 混合检索。
- LangGraph `Planner → Coder → Tester → Reviewer → Finalizer` 修复与有限轮次重试。
- 计划、文件修改范围、Docker 测试的三处 **Human-in-the-loop** 审批，SQLite Checkpointer 保存暂停点。
- MCP stdio 调用文件、代码检索、Git 与受限测试工具；Git worktree 隔离修改，Docker 运行 pytest。
- FastAPI + 原生前端：任务列表/详情、持久 SSE 进度、暂停/继续/取消、Diff 预览与确认应用。
- 原 V4 的简易只读问答模式（注意：**不是**原 V1 的多轮 CodeAgent）。

## 使用方式

建议 Python 3.13，**仅限可信的本地开发环境**。首次启动前确保 `git`、`docker` 可用，准备包含 Python/pytest 及被测项目依赖的**本地 Docker 镜像**；程序不会自动拉取或安装镜像。

```powershell
python -m pip install -r requirements.txt
Copy-Item .env.example .env
# 当前 V4 Worker 的 LLM 读取进程环境变量；只修改 .env 不保证足够。
$env:DASHSCOPE_API_KEY = "替换成你的API密钥"
python main.py
```

浏览器访问 <http://127.0.0.1:8000/>。也可以直接运行：

```powershell
python -m uvicorn web_runtime.app:app --host 127.0.0.1 --port 8000 --workers 1
```

`--workers 1` 不能增加到多进程：当前调度器在 Web 进程内维护任务进程句柄。浏览器允许提交凭据，但目前 Web Key 向 MCP 检索进程传递的问题尚未修复；如果检索不可用，请检查 `.env` 中的 `DASHSCOPE_API_KEY` 配置。

## 主要目录

| 目录 | 职责 |
| --- | --- |
| `web_runtime/` | V4 FastAPI、Worker、SQLite 任务、SSE、审批与工作区应用 |
| `graph/` | 修复节点、共享 Graph 工厂、Pydantic State、Editor 与测试结果模型 |
| `rag/`、`services/` | 代码加载、AST 切分、Chroma/混合检索、代码检索服务 |
| `mcp_servers/`、`mcp_client/` | MCP 协议及文件/检索/Git/测试工具 |
| `workspace/`、`sandbox/`、`audit/` | Git worktree、Docker pytest、审计 |
| `tests/` | AST、检索、修复图、MCP、Docker、安全与 Web 回归 |
| `docs/` | V4 使用文档、重构文档与历史参考资料 |

## 验证

```powershell
python -m compileall -q .
python -m pytest tests/test_v4_runtime.py tests/test_v4_diagnostics.py tests/test_v2_workflow.py tests/v3/test_web_workflow.py -q -p no:cacheprovider
python -m pytest tests -q -p no:cacheprovider
```

完整端到端测试依赖 **LangGraph、MCP、Chroma、Git、Docker、目标仓库、真实 LLM/Embedding API**，仅编译通过不能代表完整功能验收。部分真实 Docker 测试受环境变量控制并默认跳过。

## 重构原则

本次**仅做精简和共享 Graph 拓扑**，不改旧的 Key 传递、任务状态机、问答策略、索引生命周期、补丁审批语义及其他业务缺陷。历史 V1/V2 文档已转入 `docs/history/`，不应作为当前 V4 的运行说明。
