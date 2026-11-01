# AICodingDoctor V4 — 开发约束

- 正式产品入口：`web_runtime/app.py`（FastAPI），任务执行：`web_runtime/worker.py`。
- 保留 V4 的 LangGraph 审批、SQLite Checkpointer、MCP stdio、Git worktree、Docker pytest、SSE、Diff 预览及应用修改。
- `graph/workflow.py::build_repair_graph` 是唯一的修复图拓扑定义；`web_runtime/workflow.py` 只是 V4 兼容调用入口。
- 严禁在宿主运行未经信任仓库的代码；测试须通过受限 Docker 环境执行。
- 禁止绕过 Editor 的路径白名单、哈希校验、语法校验和人工审批。
- 当前批次只做代码瘦身，不修复既有问题；待解决问题见 `docs/REFACTOR_V4.md`。
- 源码只支持 `.py`、`.md`；不要擅自声称多语言支持。
- 修改代码应增加或保留对应测试；真实模型、Docker、MCP 测试必须明确标明是否执行。
